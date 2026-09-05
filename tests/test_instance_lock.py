import tempfile
import unittest
from pathlib import Path

from src.instance_lock import InstanceLock


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_is_rejected_until_first_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.lock"
            first = InstanceLock(path)
            second = InstanceLock(path)

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


if __name__ == "__main__":
    unittest.main()
