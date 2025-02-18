import os
import platform
import random
import sqlite3
import sys
import time

import numpy as np
import pandas as pd
import pytest
import sqlite_vec
from pandas.testing import assert_frame_equal

from feast import FeatureStore, RepoConfig
from feast.errors import FeatureViewNotFoundException
from feast.protos.feast.types.EntityKey_pb2 import EntityKey as EntityKeyProto
from feast.protos.feast.types.Value_pb2 import FloatList as FloatListProto
from feast.protos.feast.types.Value_pb2 import Value as ValueProto
from feast.repo_config import RegistryConfig
from feast.types import ValueType
from feast.utils import _utc_now
from tests.integration.feature_repos.universal.feature_views import TAGS
from tests.utils.cli_repo_creator import CliRunner, get_example_repo


def test_get_online_features() -> None:
    """
    Test reading from the online store in local mode.
    """
    runner = CliRunner()
    with runner.local_repo(
        get_example_repo("example_feature_repo_1.py"),
        "file",
    ) as store:
        # Write some data to two tables
        driver_locations_fv = store.get_feature_view(name="driver_locations")
        customer_profile_fv = store.get_feature_view(name="customer_profile")
        customer_driver_combined_fv = store.get_feature_view(
            name="customer_driver_combined"
        )

        provider = store._get_provider()

        driver_key = EntityKeyProto(
            join_keys=["driver_id"], entity_values=[ValueProto(int64_val=1)]
        )
        provider.online_write_batch(
            config=store.config,
            table=driver_locations_fv,
            data=[
                (
                    driver_key,
                    {
                        "lat": ValueProto(double_val=0.1),
                        "lon": ValueProto(string_val="1.0"),
                    },
                    _utc_now(),
                    _utc_now(),
                )
            ],
            progress=None,
        )

        customer_key = EntityKeyProto(
            join_keys=["customer_id"], entity_values=[ValueProto(string_val="5")]
        )
        provider.online_write_batch(
            config=store.config,
            table=customer_profile_fv,
            data=[
                (
                    customer_key,
                    {
                        "avg_orders_day": ValueProto(float_val=1.0),
                        "name": ValueProto(string_val="John"),
                        "age": ValueProto(int64_val=3),
                    },
                    _utc_now(),
                    _utc_now(),
                )
            ],
            progress=None,
        )

        customer_key = EntityKeyProto(
            join_keys=["customer_id", "driver_id"],
            entity_values=[ValueProto(string_val="5"), ValueProto(int64_val=1)],
        )
        provider.online_write_batch(
            config=store.config,
            table=customer_driver_combined_fv,
            data=[
                (
                    customer_key,
                    {"trips": ValueProto(int64_val=7)},
                    _utc_now(),
                    _utc_now(),
                )
            ],
            progress=None,
        )

        assert len(store.list_entities()) == 3
        assert len(store.list_entities(tags=TAGS)) == 2

        # Retrieve two features using two keys, one valid one non-existing
        result = store.get_online_features(
            features=[
                "driver_locations:lon",
                "customer_profile:avg_orders_day",
                "customer_profile:name",
                "customer_driver_combined:trips",
            ],
            entity_rows=[
                {"driver_id": 1, "customer_id": "5"},
                {"driver_id": 1, "customer_id": 5},
            ],
            full_feature_names=False,
        ).to_dict()

        assert "lon" in result
        assert "avg_orders_day" in result
        assert "name" in result
        assert result["driver_id"] == [1, 1]
        assert result["customer_id"] == ["5", "5"]
        assert result["lon"] == ["1.0", "1.0"]
        assert result["avg_orders_day"] == [1.0, 1.0]
        assert result["name"] == ["John", "John"]
        assert result["trips"] == [7, 7]

        # Ensure features are still in result when keys not found
        result = store.get_online_features(
            features=["customer_driver_combined:trips"],
            entity_rows=[{"driver_id": 0, "customer_id": 0}],
            full_feature_names=False,
        ).to_dict()

        assert "trips" in result

        with pytest.raises(KeyError) as excinfo:
            _ = store.get_online_features(
                features=["driver_locations:lon"],
                entity_rows=[{"customer_id": 0}],
                full_feature_names=False,
            ).to_dict()

        error_message = str(excinfo.value)
        assert "Missing join key values for keys:" in error_message
        assert (
            "Missing join key values for keys: ['customer_id', 'driver_id', 'item_id']."
            in error_message
        )
        assert "Provided join_key_values: ['customer_id']" in error_message

        result = store.get_online_features(
            features=["customer_profile_pandas_odfv:on_demand_age"],
            entity_rows=[{"driver_id": 1, "customer_id": "5"}],
            full_feature_names=False,
        ).to_dict()

        assert "on_demand_age" in result
        assert result["driver_id"] == [1]
        assert result["customer_id"] == ["5"]
        assert result["on_demand_age"] == [4]

        # invalid table reference
        with pytest.raises(FeatureViewNotFoundException):
            store.get_online_features(
                features=["driver_locations_bad:lon"],
                entity_rows=[{"driver_id": 1}],
                full_feature_names=False,
            )

        # Create new FeatureStore object with fast cache invalidation
        cache_ttl = 1
        fs_fast_ttl = FeatureStore(
            config=RepoConfig(
                registry=RegistryConfig(
                    path=store.config.registry.path, cache_ttl_seconds=cache_ttl
                ),
                online_store=store.config.online_store,
                project=store.project,
                provider=store.config.provider,
                entity_key_serialization_version=2,
            )
        )

        # Should download the registry and cache it permanently (or until manually refreshed)
        result = fs_fast_ttl.get_online_features(
            features=[
                "driver_locations:lon",
                "customer_profile:avg_orders_day",
                "customer_profile:name",
                "customer_driver_combined:trips",
            ],
            entity_rows=[{"driver_id": 1, "customer_id": 5}],
            full_feature_names=False,
        ).to_dict()
        assert result["lon"] == ["1.0"]
        assert result["trips"] == [7]

        # Rename the registry.db so that it cant be used for refreshes
        os.rename(store.config.registry.path, store.config.registry.path + "_fake")

        # Wait for registry to expire
        time.sleep(cache_ttl)

        # Will try to reload registry because it has expired (it will fail because we deleted the actual registry file)
        with pytest.raises(FileNotFoundError):
            fs_fast_ttl.get_online_features(
                features=[
                    "driver_locations:lon",
                    "customer_profile:avg_orders_day",
                    "customer_profile:name",
                    "customer_driver_combined:trips",
                ],
                entity_rows=[{"driver_id": 1, "customer_id": 5}],
                full_feature_names=False,
            ).to_dict()

        # Restore registry.db so that we can see if it actually reloads registry
        os.rename(store.config.registry.path + "_fake", store.config.registry.path)

        # Test if registry is actually reloaded and whether results return
        result = fs_fast_ttl.get_online_features(
            features=[
                "driver_locations:lon",
                "customer_profile:avg_orders_day",
                "customer_profile:name",
                "customer_driver_combined:trips",
            ],
            entity_rows=[{"driver_id": 1, "customer_id": 5}],
            full_feature_names=False,
        ).to_dict()
        assert result["lon"] == ["1.0"]
        assert result["trips"] == [7]

        # Create a registry with infinite cache (for users that want to manually refresh the registry)
        fs_infinite_ttl = FeatureStore(
            config=RepoConfig(
                registry=RegistryConfig(
                    path=store.config.registry.path, cache_ttl_seconds=0
                ),
                online_store=store.config.online_store,
                project=store.project,
                provider=store.config.provider,
                entity_key_serialization_version=2,
            )
        )

        # Should return results (and fill the registry cache)
        result = fs_infinite_ttl.get_online_features(
            features=[
                "driver_locations:lon",
                "customer_profile:avg_orders_day",
                "customer_profile:name",
                "customer_driver_combined:trips",
            ],
            entity_rows=[{"driver_id": 1, "customer_id": 5}],
            full_feature_names=False,
        ).to_dict()
        assert result["lon"] == ["1.0"]
        assert result["trips"] == [7]

        # Wait a bit so that an arbitrary TTL would take effect
        time.sleep(2)

        # Rename the registry.db so that it cant be used for refreshes
        os.rename(store.config.registry.path, store.config.registry.path + "_fake")

        # TTL is infinite so this method should use registry cache
        result = fs_infinite_ttl.get_online_features(
            features=[
                "driver_locations:lon",
                "customer_profile:avg_orders_day",
                "customer_profile:name",
                "customer_driver_combined:trips",
            ],
            entity_rows=[{"driver_id": 1, "customer_id": 5}],
            full_feature_names=False,
        ).to_dict()
        assert result["lon"] == ["1.0"]
        assert result["trips"] == [7]

        # Force registry reload (should fail because file is missing)
        with pytest.raises(FileNotFoundError):
            fs_infinite_ttl.refresh_registry()

        # Restore registry.db so that teardown works
        os.rename(store.config.registry.path + "_fake", store.config.registry.path)


def test_get_online_features_milvus() -> None:
    """
    Test reading from the online store in local mode.
    """
    runner = CliRunner()
    with runner.local_repo(
        get_example_repo("example_feature_repo_1.py"),
        offline_store="file",
        online_store="milvus",
        apply=False,
        teardown=False,
    ) as store:
        from tests.example_repos.example_feature_repo_1 import (
            all_drivers_feature_service,
            customer,
            customer_driver_combined,
            customer_driver_combined_source,
            customer_profile,
            customer_profile_pandas_odfv,
            customer_profile_source,
            driver,
            driver_locations,
            driver_locations_source,
            item,
            pushed_driver_locations,
            rag_documents_source,
        )

        store.apply(
            [
                driver_locations_source,
                customer_profile_source,
                customer_driver_combined_source,
                rag_documents_source,
                driver,
                customer,
                item,
                driver_locations,
                pushed_driver_locations,
                customer_profile,
                customer_driver_combined,
                # document_embeddings,
                customer_profile_pandas_odfv,
                all_drivers_feature_service,
            ]
        )

        # Write some data to two tables
        driver_locations_fv = store.get_feature_view(name="driver_locations")
        customer_profile_fv = store.get_feature_view(name="customer_profile")
        customer_driver_combined_fv = store.get_feature_view(
            name="customer_driver_combined"
        )

        provider = store._get_provider()

        driver_key = EntityKeyProto(
            join_keys=["driver_id"], entity_values=[ValueProto(int64_val=1)]
        )
        provider.online_write_batch(
            config=store.config,
            table=driver_locations_fv,
            data=[
                (
                    driver_key,
                    {
                        "lat": ValueProto(double_val=0.1),
                        "lon": ValueProto(string_val="1.0"),
                    },
                    _utc_now(),
                    _utc_now(),
                )
            ],
            progress=None,
        )

        customer_key = EntityKeyProto(
            join_keys=["customer_id"], entity_values=[ValueProto(string_val="5")]
        )
        provider.online_write_batch(
            config=store.config,
            table=customer_profile_fv,
            data=[
                (
                    customer_key,
                    {
                        "avg_orders_day": ValueProto(float_val=1.0),
                        "name": ValueProto(string_val="John"),
                        "age": ValueProto(int64_val=3),
                    },
                    _utc_now(),
                    _utc_now(),
                )
            ],
            progress=None,
        )

        customer_key = EntityKeyProto(
            join_keys=["customer_id", "driver_id"],
            entity_values=[ValueProto(string_val="5"), ValueProto(int64_val=1)],
        )
        provider.online_write_batch(
            config=store.config,
            table=customer_driver_combined_fv,
            data=[
                (
                    customer_key,
                    {"trips": ValueProto(int64_val=7)},
                    _utc_now(),
                    _utc_now(),
                )
            ],
            progress=None,
        )

        assert len(store.list_entities()) == 3
        assert len(store.list_entities(tags=TAGS)) == 2

        # Retrieve two features using two keys, one valid one non-existing
        result = store.get_online_features(
            features=[
                "driver_locations:lon",
                "customer_profile:avg_orders_day",
                "customer_profile:name",
                "customer_driver_combined:trips",
            ],
            entity_rows=[
                {"driver_id": 1, "customer_id": "5"},
                {"driver_id": 1, "customer_id": 5},
            ],
            full_feature_names=False,
        ).to_dict()

        assert "lon" in result
        assert "avg_orders_day" in result
        assert "name" in result
        assert result["driver_id"] == [1, 1]
        assert result["customer_id"] == ["5", "5"]
        assert result["lon"] == ["1.0", "1.0"]
        assert result["avg_orders_day"] == [1.0, 1.0]
        assert result["name"] == ["John", "John"]
        assert result["trips"] == [7, 7]

        # Ensure features are still in result when keys not found
        result = store.get_online_features(
            features=["customer_driver_combined:trips"],
            entity_rows=[{"driver_id": 0, "customer_id": 0}],
            full_feature_names=False,
        ).to_dict()

        assert "trips" in result

        result = store.get_online_features(
            features=["customer_profile_pandas_odfv:on_demand_age"],
            entity_rows=[{"driver_id": 1, "customer_id": "5"}],
            full_feature_names=False,
        ).to_dict()

        assert "on_demand_age" in result
        assert result["driver_id"] == [1]
        assert result["customer_id"] == ["5"]
        assert result["on_demand_age"] == [4]

        # invalid table reference
        with pytest.raises(FeatureViewNotFoundException):
            store.get_online_features(
                features=["driver_locations_bad:lon"],
                entity_rows=[{"driver_id": 1}],
                full_feature_names=False,
            )


def test_online_to_df():
    """
    Test dataframe conversion. Make sure the response columns and rows are
    the same order as the request.
    """
    driver_ids = [1, 2, 3]
    customer_ids = [4, 5, 6]
    name = "foo"
    lon_multiply = 1.0
    lat_multiply = 0.1
    age_multiply = 10
    avg_order_day_multiply = 1.0

    runner = CliRunner()
    with runner.local_repo(
        get_example_repo("example_feature_repo_1.py"), "file"
    ) as store:
        # Write three tables to online store
        driver_locations_fv = store.get_feature_view(name="driver_locations")
        customer_profile_fv = store.get_feature_view(name="customer_profile")
        customer_driver_combined_fv = store.get_feature_view(
            name="customer_driver_combined"
        )
        provider = store._get_provider()

        for d, c in zip(driver_ids, customer_ids):
            """
            driver table:
                                    lon                    lat
                1                   1.0                    0.1
                2                   2.0                    0.2
                3                   3.0                    0.3
            """
            driver_key = EntityKeyProto(
                join_keys=["driver_id"], entity_values=[ValueProto(int64_val=d)]
            )
            provider.online_write_batch(
                config=store.config,
                table=driver_locations_fv,
                data=[
                    (
                        driver_key,
                        {
                            "lat": ValueProto(double_val=d * lat_multiply),
                            "lon": ValueProto(string_val=str(d * lon_multiply)),
                        },
                        _utc_now(),
                        _utc_now(),
                    )
                ],
                progress=None,
            )

            """
            customer table
            customer     avg_orders_day          name        age
                4           4.0                  foo4         40
                5           5.0                  foo5         50
                6           6.0                  foo6         60
            """
            customer_key = EntityKeyProto(
                join_keys=["customer_id"], entity_values=[ValueProto(string_val=str(c))]
            )
            provider.online_write_batch(
                config=store.config,
                table=customer_profile_fv,
                data=[
                    (
                        customer_key,
                        {
                            "avg_orders_day": ValueProto(
                                float_val=c * avg_order_day_multiply
                            ),
                            "name": ValueProto(string_val=name + str(c)),
                            "age": ValueProto(int64_val=c * age_multiply),
                        },
                        _utc_now(),
                        _utc_now(),
                    )
                ],
                progress=None,
            )
            """
            customer_driver_combined table
            customer  driver    trips
                4       1       4
                5       2       10
                6       3       18
            """
            combo_keys = EntityKeyProto(
                join_keys=["customer_id", "driver_id"],
                entity_values=[ValueProto(string_val=str(c)), ValueProto(int64_val=d)],
            )
            provider.online_write_batch(
                config=store.config,
                table=customer_driver_combined_fv,
                data=[
                    (
                        combo_keys,
                        {"trips": ValueProto(int64_val=c * d)},
                        _utc_now(),
                        _utc_now(),
                    )
                ],
                progress=None,
            )

        # Get online features in dataframe
        result_df = store.get_online_features(
            features=[
                "driver_locations:lon",
                "driver_locations:lat",
                "customer_profile:avg_orders_day",
                "customer_profile:name",
                "customer_profile:age",
                "customer_driver_combined:trips",
            ],
            # Reverse the row order
            entity_rows=[
                {"driver_id": d, "customer_id": c}
                for (d, c) in zip(reversed(driver_ids), reversed(customer_ids))
            ],
        ).to_df()
        """
        Construct the expected dataframe with reversed row order like so:
        driver  customer     lon    lat     avg_orders_day      name        age     trips
            3       6        3.0    0.3         6.0             foo6        60       18
            2       5        2.0    0.2         5.0             foo5        50       10
            1       4        1.0    0.1         4.0             foo4        40       4
        """
        df_dict = {
            "driver_id": driver_ids,
            "customer_id": [str(c) for c in customer_ids],
            "lon": [str(d * lon_multiply) for d in driver_ids],
            "lat": [d * lat_multiply for d in driver_ids],
            "avg_orders_day": [c * avg_order_day_multiply for c in customer_ids],
            "name": [name + str(c) for c in customer_ids],
            "age": [c * age_multiply for c in customer_ids],
            "trips": [d * c for (d, c) in zip(driver_ids, customer_ids)],
        }
        # Requested column order
        ordered_column = [
            "driver_id",
            "customer_id",
            "lon",
            "lat",
            "avg_orders_day",
            "name",
            "age",
            "trips",
        ]
        expected_df = pd.DataFrame({k: reversed(v) for (k, v) in df_dict.items()})
        assert_frame_equal(result_df[ordered_column], expected_df)


@pytest.mark.skipif(
    sys.version_info[0:2] != (3, 10) or platform.system() != "Darwin",
    reason="Only works on Python 3.10 and MacOS",
)
def test_sqlite_get_online_documents() -> None:
    """
    Test retrieving documents from the online store in local mode.
    """
    n = 10  # number of samples - note: we'll actually double it
    vector_length = 8
    runner = CliRunner()
    with runner.local_repo(
        get_example_repo("example_feature_repo_1.py"), "file"
    ) as store:
        store.config.online_store.vector_enabled = True
        store.config.online_store.vector_len = vector_length
        # Write some data to two tables
        document_embeddings_fv = store.get_feature_view(name="document_embeddings")

        provider = store._get_provider()

        item_keys = [
            EntityKeyProto(
                join_keys=["item_id"], entity_values=[ValueProto(int64_val=i)]
            )
            for i in range(n)
        ]
        data = []
        for item_key in item_keys:
            data.append(
                (
                    item_key,
                    {
                        "Embeddings": ValueProto(
                            float_list_val=FloatListProto(
                                val=np.random.random(
                                    vector_length,
                                )
                            )
                        )
                    },
                    _utc_now(),
                    _utc_now(),
                )
            )

        provider.online_write_batch(
            config=store.config,
            table=document_embeddings_fv,
            data=data,
            progress=None,
        )
        documents_df = pd.DataFrame(
            {
                "item_id": [str(i) for i in range(n)],
                "Embeddings": [
                    np.random.random(
                        vector_length,
                    )
                    for i in range(n)
                ],
                "event_timestamp": [_utc_now() for _ in range(n)],
            }
        )

        store.write_to_online_store(
            feature_view_name="document_embeddings",
            df=documents_df,
        )

        document_table = store._provider._online_store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' and name like '%_document_embeddings';"
        ).fetchall()
        assert len(document_table) == 1
        document_table_name = document_table[0][0]
        record_count = len(
            store._provider._online_store._conn.execute(
                f"select * from {document_table_name}"
            ).fetchall()
        )
        assert record_count == len(data) + documents_df.shape[0]

        query_embedding = np.random.random(
            vector_length,
        )
        result = store.retrieve_online_documents(
            feature="document_embeddings:Embeddings", query=query_embedding, top_k=3
        ).to_dict()

        assert "Embeddings" in result
        assert "distance" in result
        assert len(result["distance"]) == 3


@pytest.mark.skipif(
    sys.version_info[0:2] != (3, 10) or platform.system() != "Darwin",
    reason="Only works on Python 3.10 and MacOS",
)
def test_sqlite_vec_import() -> None:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)

    db.execute("""
    create virtual table vec_examples using vec0(
      sample_embedding float[8]
    );
    """)

    db.execute("""
    insert into vec_examples(rowid, sample_embedding)
    values
        (1, '[-0.200, 0.250, 0.341, -0.211, 0.645, 0.935, -0.316, -0.924]'),
        (2, '[0.443, -0.501, 0.355, -0.771, 0.707, -0.708, -0.185, 0.362]'),
        (3, '[0.716, -0.927, 0.134, 0.052, -0.669, 0.793, -0.634, -0.162]'),
        (4, '[-0.710, 0.330, 0.656, 0.041, -0.990, 0.726, 0.385, -0.958]');
    """)

    sqlite_version, vec_version = db.execute(
        "select sqlite_version(), vec_version()"
    ).fetchone()
    print(f"sqlite_version={sqlite_version}, vec_version={vec_version}")

    result = db.execute("""
        select
            rowid,
            distance
        from vec_examples
        where sample_embedding match '[0.890, 0.544, 0.825, 0.961, 0.358, 0.0196, 0.521, 0.175]'
        order by distance
        limit 2;
    """).fetchall()
    result = [(rowid, round(distance, 2)) for rowid, distance in result]
    assert result == [(2, 2.39), (1, 2.39)]


@pytest.mark.skipif(
    sys.version_info[0:2] != (3, 10),
    reason="Only works on Python 3.10",
)
def test_sqlite_get_online_documents_v2() -> None:
    """Test retrieving documents using v2 method with vector similarity search."""
    n = 10
    vector_length = 8
    runner = CliRunner()
    with runner.local_repo(
        get_example_repo("example_feature_repo_1.py"), "file"
    ) as store:
        store.config.online_store.vector_enabled = True
        store.config.online_store.vector_len = vector_length
        store.config.entity_key_serialization_version = 3
        document_embeddings_fv = store.get_feature_view(name="document_embeddings")

        provider = store._get_provider()

        # Create test data
        item_keys = [
            EntityKeyProto(
                join_keys=["item_id"], entity_values=[ValueProto(int64_val=i)]
            )
            for i in range(n)
        ]
        data = []
        for item_key in item_keys:
            data.append(
                (
                    item_key,
                    {
                        "Embeddings": ValueProto(
                            float_list_val=FloatListProto(
                                val=[float(x) for x in np.random.random(vector_length)]
                            )
                        )
                    },
                    _utc_now(),
                    _utc_now(),
                )
            )

        provider.online_write_batch(
            config=store.config,
            table=document_embeddings_fv,
            data=data,
            progress=None,
        )

        # Test vector similarity search
        query_embedding = [float(x) for x in np.random.random(vector_length)]
        result = store.retrieve_online_documents_v2(
            features=["document_embeddings:Embeddings"],
            query=query_embedding,
            top_k=3,
        ).to_dict()

        assert "Embeddings" in result
        assert "distance" in result
        assert len(result["distance"]) == 3


@pytest.mark.skip(reason="Skipping this test as CI struggles with it")
def test_local_milvus() -> None:
    import random

    from pymilvus import MilvusClient

    random.seed(42)
    VECTOR_LENGTH: int = 768
    COLLECTION_NAME: str = "test_demo_collection"

    client = MilvusClient("./milvus_demo.db")

    for collection in client.list_collections():
        client.drop_collection(collection_name=collection)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        dimension=VECTOR_LENGTH,
    )
    assert client.list_collections() == [COLLECTION_NAME]

    docs = [
        "Artificial intelligence was founded as an academic discipline in 1956.",
        "Alan Turing was the first person to conduct substantial research in AI.",
        "Born in Maida Vale, London, Turing was raised in southern England.",
    ]
    # Use fake representation with random vectors (vector_length dimension).
    vectors = [[random.uniform(-1, 1) for _ in range(VECTOR_LENGTH)] for _ in docs]
    data = [
        {"id": i, "vector": vectors[i], "text": docs[i], "subject": "history"}
        for i in range(len(vectors))
    ]

    print("Data has", len(data), "entities, each with fields: ", data[0].keys())
    print("Vector dim:", len(data[0]["vector"]))

    insert_res = client.insert(collection_name=COLLECTION_NAME, data=data)
    assert insert_res == {"insert_count": 3, "ids": [0, 1, 2], "cost": 0}

    query_vectors = [[random.uniform(-1, 1) for _ in range(VECTOR_LENGTH)]]

    search_res = client.search(
        collection_name=COLLECTION_NAME,  # target collection
        data=query_vectors,  # query vectors
        limit=2,  # number of returned entities
        output_fields=["text", "subject"],  # specifies fields to be returned
    )
    assert [j["id"] for j in search_res[0]] == [0, 1]
    query_result = client.query(
        collection_name=COLLECTION_NAME,
        filter="id == 0",
    )
    assert list(query_result[0].keys()) == ["id", "text", "subject", "vector"]

    client.drop_collection(collection_name=COLLECTION_NAME)


def test_milvus_lite_get_online_documents_v2() -> None:
    """
    Test retrieving documents from the online store in local mode.
    """

    random.seed(42)
    n = 10  # number of samples - note: we'll actually double it
    vector_length = 10
    runner = CliRunner()
    with runner.local_repo(
        example_repo_py=get_example_repo("example_rag_feature_repo.py"),
        offline_store="file",
        online_store="milvus",
        apply=False,
        teardown=False,
    ) as store:
        from datetime import timedelta

        from feast import Entity, FeatureView, Field, FileSource
        from feast.types import Array, Float32, Int64, String, UnixTimestamp

        # This is for Milvus
        # Note that file source paths are not validated, so there doesn't actually need to be any data
        # at the paths for these file sources. Since these paths are effectively fake, this example
        # feature repo should not be used for historical retrieval.

        rag_documents_source = FileSource(
            path="data/embedded_documents.parquet",
            timestamp_field="event_timestamp",
            created_timestamp_column="created_timestamp",
        )

        item = Entity(
            name="item_id",  # The name is derived from this argument, not object name.
            join_keys=["item_id"],
            value_type=ValueType.INT64,
        )
        author = Entity(
            name="author_id",
            join_keys=["author_id"],
            value_type=ValueType.STRING,
        )

        document_embeddings = FeatureView(
            name="embedded_documents",
            entities=[item, author],
            schema=[
                Field(
                    name="vector",
                    dtype=Array(Float32),
                    vector_index=True,
                    vector_search_metric="COSINE",
                ),
                Field(name="item_id", dtype=Int64),
                Field(name="author_id", dtype=String),
                Field(name="created_timestamp", dtype=UnixTimestamp),
                Field(name="sentence_chunks", dtype=String),
                Field(name="event_timestamp", dtype=UnixTimestamp),
            ],
            source=rag_documents_source,
            ttl=timedelta(hours=24),
        )

        store.apply([rag_documents_source, item, document_embeddings])

        # Write some data to two tables
        document_embeddings_fv = store.get_feature_view(name="embedded_documents")

        provider = store._get_provider()

        item_keys = [
            EntityKeyProto(
                join_keys=["item_id", "author_id"],
                entity_values=[
                    ValueProto(int64_val=i),
                    ValueProto(string_val=f"author_{i}"),
                ],
            )
            for i in range(n)
        ]
        data = []
        for i, item_key in enumerate(item_keys):
            data.append(
                (
                    item_key,
                    {
                        "vector": ValueProto(
                            float_list_val=FloatListProto(
                                val=np.random.random(
                                    vector_length,
                                )
                                + i
                            )
                        ),
                        "sentence_chunks": ValueProto(string_val=f"sentence chunk {i}"),
                    },
                    _utc_now(),
                    _utc_now(),
                )
            )

        provider.online_write_batch(
            config=store.config,
            table=document_embeddings_fv,
            data=data,
            progress=None,
        )
        documents_df = pd.DataFrame(
            {
                "item_id": [str(i) for i in range(n)],
                "author_id": [f"author_{i}" for i in range(n)],
                "vector": [
                    np.random.random(
                        vector_length,
                    )
                    + i
                    for i in range(n)
                ],
                "sentence_chunks": [f"sentence chunk {i}" for i in range(n)],
                "event_timestamp": [_utc_now() for _ in range(n)],
                "created_timestamp": [_utc_now() for _ in range(n)],
            }
        )

        store.write_to_online_store(
            feature_view_name="embedded_documents",
            df=documents_df,
        )

        query_embedding = np.random.random(
            vector_length,
        )

        client = store._provider._online_store.client
        collection_name = client.list_collections()[0]
        search_params = {
            "metric_type": "COSINE",
            "params": {"nprobe": 10},
        }

        results = client.search(
            collection_name=collection_name,
            data=[query_embedding],
            anns_field="vector",
            search_params=search_params,
            limit=3,
            output_fields=[
                "item_id",
                "author_id",
                "sentence_chunks",
                "created_ts",
                "event_ts",
            ],
        )
        result = store.retrieve_online_documents_v2(
            features=[
                "embedded_documents:vector",
                "embedded_documents:item_id",
                "embedded_documents:author_id",
                "embedded_documents:sentence_chunks",
            ],
            query=query_embedding,
            top_k=3,
        ).to_dict()

        for k in ["vector", "item_id", "author_id", "sentence_chunks", "distance"]:
            assert k in result, f"Missing {k} in retrieve_online_documents response"
        assert len(result["distance"]) == len(results[0])


def test_milvus_native_from_feast_data() -> None:
    import random
    from datetime import datetime

    import numpy as np
    from pymilvus import MilvusClient

    random.seed(42)
    VECTOR_LENGTH = 10  # Matches vector_length from the Feast example
    COLLECTION_NAME = "embedded_documents"

    # Initialize Milvus client with local setup
    client = MilvusClient("./milvus_demo.db")

    # Clear and recreate collection
    for collection in client.list_collections():
        client.drop_collection(collection_name=collection)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        dimension=VECTOR_LENGTH,
        metric_type="COSINE",  # Matches Feast's vector_search_metric
    )
    assert client.list_collections() == [COLLECTION_NAME]

    # Prepare data for insertion, similar to the Feast example
    n = 10  # Number of items
    data = []
    for i in range(n):
        vector = (np.random.random(VECTOR_LENGTH) + i).tolist()
        data.append(
            {
                "id": i,
                "vector": vector,
                "item_id": i,
                "author_id": f"author_{i}",
                "sentence_chunks": f"sentence chunk {i}",
                "event_timestamp": datetime.utcnow().isoformat(),
                "created_timestamp": datetime.utcnow().isoformat(),
            }
        )

    print("Data has", len(data), "entities, each with fields:", data[0].keys())

    # Insert data into Milvus
    insert_res = client.insert(collection_name=COLLECTION_NAME, data=data)
    assert insert_res == {"insert_count": n, "ids": list(range(n)), "cost": 0}

    # Perform a vector search using a random query embedding
    query_embedding = (np.random.random(VECTOR_LENGTH)).tolist()
    search_res = client.search(
        collection_name=COLLECTION_NAME,
        data=[query_embedding],
        limit=3,  # Top 3 results
        output_fields=["item_id", "author_id", "sentence_chunks"],
    )

    # Validate the search results
    assert len(search_res[0]) == 3
    print("Search Results:", search_res[0])

    # Clean up the collection
    client.drop_collection(collection_name=COLLECTION_NAME)
