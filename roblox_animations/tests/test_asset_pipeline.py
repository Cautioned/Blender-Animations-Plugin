import unittest
import gc
import weakref

from ..core.asset_pipeline import AssetRegistry, AssetState


class AssetRegistryTests(unittest.TestCase):
    def test_consumer_is_published_once_after_all_assets_finish(self):
        registry = AssetRegistry()
        registry.register("asset:1", "rbxassetid://1", "texture")
        registry.register("asset:2", "rbxassetid://2", "texture")
        self.assertFalse(registry.subscribe("material", ("asset:1", "asset:2")))

        registry.finish("asset:1", payload=b"one")
        self.assertEqual(registry.drain_ready_consumers(), [])
        registry.finish("asset:2", error="missing")
        self.assertEqual(registry.drain_ready_consumers(), ["material"])
        registry.finish("asset:2", error="duplicate")
        self.assertEqual(registry.drain_ready_consumers(), [])

    def test_terminal_assets_make_late_consumer_immediately_ready(self):
        registry = AssetRegistry()
        registry.register("asset:1", "rbxassetid://1", "texture")
        registry.finish("asset:1", payload=b"one")
        self.assertTrue(registry.subscribe("late", ("asset:1",)))
        self.assertEqual(registry.get("asset:1").state, AssetState.ready)

    def test_completion_queue_contains_each_asset_once(self):
        registry = AssetRegistry()
        registry.register("asset:1", "rbxassetid://1", "texture")
        registry.finish("asset:1", payload=b"one")
        registry.finish("asset:1", payload=b"two")
        completions = registry.drain_completions()
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0].payload, b"one")

    def test_clear_releases_payloads_and_dependency_edges(self):
        class Payload:
            pass

        registry = AssetRegistry()
        registry.register("asset:1", "rbxassetid://1", "texture")
        registry.subscribe("material", ("asset:1",))
        payload = Payload()
        payload_ref = weakref.ref(payload)
        registry.finish("asset:1", payload=payload)
        del payload

        registry.clear()
        gc.collect()

        self.assertIsNone(payload_ref())
        self.assertEqual(sum(registry.counts().values()), 0)
        self.assertEqual(registry.drain_completions(), [])
        self.assertEqual(registry.drain_ready_consumers(), [])


if __name__ == "__main__":
    unittest.main()
