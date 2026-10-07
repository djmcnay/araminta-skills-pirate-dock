import ast, asyncio, json, re, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from fastapi import HTTPException
from pydantic import BaseModel, Field
import httpx

SERVER=Path(__file__).resolve().parents[1]/'scripts/server.py'
def load():
 tree=ast.parse(SERVER.read_text())
 nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and (getattr(n,'name','').startswith(('_queue','_action','_payload')) or getattr(n,'name','') in ('QueueActionRequest','queue_pause','queue_resume','queue_transfer','queue_delete'))]
 for n in nodes:n.decorator_list=[]
 ns=dict(Path=Path,BaseModel=BaseModel,Field=Field,HTTPException=HTTPException,httpx=httpx,asyncio=asyncio,re=re,json=json,DOWNLOAD_DIR=Path('/downloads'))
 exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SERVER),'exec'),ns)
 return ns
class ActionTests(unittest.TestCase):
 def test_queue_exposes_files(self):
  ns=load(); row=ns['_queue_item']({}, {'gid':'abc','files':[{'path':'/downloads/pack/movie.mkv','length':'2048','completedLength':'1024'}]})
  self.assertEqual(row.get('files'),[{'path':'pack/movie.mkv','length':2048,'completed':1024}])
 def test_no_live_files_are_unknown(self):
  self.assertEqual(load()['_queue_item']({},None).get('files'),[])
class EndpointTests(unittest.TestCase):
 def test_pause_and_resume_use_exact_gid(self):
  ns=load()
  self.assertIn('queue_pause',ns)
  for name,method in [('queue_pause','pause'),('queue_resume','unpause')]:
   ns['_action_rpc']=AsyncMock(return_value='abc')
   asyncio.run(ns[name]('job',ns['QueueActionRequest'](gid='0123456789abcdef')))
   self.assertEqual(ns['_action_rpc'].call_args.args,('job','0123456789abcdef',method))
 def test_delete_requires_confirmation(self):
  ns=load(); self.assertIn('queue_delete',ns)
  with self.assertRaises(HTTPException) as e:
   asyncio.run(ns['queue_delete']('job',ns['QueueActionRequest'](gid='0123456789abcdef')))
  self.assertEqual(e.exception.status_code,400)
 def test_transfer_requires_completed_payload(self):
  ns=load(); self.assertIn('queue_transfer',ns)
  ns['_action_job']=AsyncMock(return_value=({}, {'aria2_status':'active'}))
  with self.assertRaises(HTTPException) as e:
   asyncio.run(ns['queue_transfer']('job',ns['QueueActionRequest'](gid='0123456789abcdef')))
  self.assertEqual(e.exception.status_code,409)
 def test_missing_job_is_not_actionable(self):
  ns=load(); self.assertIn('_action_job',ns)
  ns['_load_torrent_status_jobs']=lambda:{}
  with self.assertRaises(HTTPException) as e:asyncio.run(ns['_action_job']('missing','0123456789abcdef'))
  self.assertEqual(e.exception.status_code,404)
if __name__=='__main__':unittest.main()
