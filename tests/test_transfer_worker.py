import importlib.util, unittest
from pathlib import Path
P=Path(__file__).resolve().parents[1]/'scripts/dock-transfer-worker.py'
class TransferTests(unittest.TestCase):
 def test_rejects_unsafe_transfer(self):
  self.assertTrue(P.exists(),'transfer worker must exist')
  spec=importlib.util.spec_from_file_location('worker',P); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
  for item in ('../Media', '/etc', '.hidden', ''):
   with self.assertRaises(ValueError):m.validate_request({'item':item,'files':[]})
if __name__=='__main__':unittest.main()
