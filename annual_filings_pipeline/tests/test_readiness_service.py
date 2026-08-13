import unittest

from app.services.readiness_service import readiness


class ReadinessTests(unittest.TestCase):
    def test_ready_requires_database_and_gpu(self):
        result = readiness(lambda: True, lambda: True)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["components"]["database"])
        self.assertTrue(result["components"]["gpu_reranker"])
        self.assertFalse(result["database_writes"])

    def test_gpu_offline_is_degraded_without_error_details(self):
        result = readiness(lambda: True, lambda: False)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(
            result["components"],
            {"database": True, "gpu_reranker": False},
        )
        self.assertNotIn("error", result)

    def test_dependency_exception_is_contained(self):
        def fail():
            raise RuntimeError("secret internal detail")

        result = readiness(fail, lambda: True)
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["components"]["database"])
        self.assertNotIn("secret", str(result))


if __name__ == "__main__":
    unittest.main()
