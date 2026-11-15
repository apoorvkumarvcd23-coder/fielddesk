"""Clean isolated browser → learned RAG → opt-in review → approved ticket demonstration."""
import argparse
from contextlib import nullcontext
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.request import urlopen
import httpx
from urllib.parse import urlsplit
from app import create_app
from demo_indexing import disposable_postgres
from embeddings import OllamaEmbedder
from generation import OllamaGenerator
from rag import add_identity, set_grant

ROOT = Path(__file__).resolve().parent

def demonstrate(url, directory):
    embedder, generator = OllamaEmbedder(), OllamaGenerator('qwen3.5:9b')
    legacy_user, legacy_operator = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    app = create_app(url, legacy_user, legacy_operator, embedder, generator)
    db, knowledge = app.state.db, app.state.knowledge
    accounts = {}
    for name, role in [('alice','user'),('bob','user'),('operator','operator'),('admin','admin')]:
        token = secrets.token_urlsafe(32)
        accounts[name] = {'id':add_identity(db,name,role,token),'role':role,'token':token}
    manuals = json.loads((ROOT/'fixtures/rag_manuals.json').read_text(encoding='utf-8'))
    private = knowledge.ingest(manuals[1]['source_key'],manuals[1]['title'],manuals[1]['body'],embedder.space.id)
    set_grant(db,private['document_id'],accounts['bob']['id'],True)
    with TemporaryDirectory(prefix='fielddesk-browser-private-') as temporary:
        credentials = Path(temporary)/'credentials.json'
        descriptor = os.open(credentials,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w',encoding='utf-8') as output:
            json.dump(accounts,output)
        environment = {**os.environ,'DATABASE_URL':url,
            'FIELDDESK_CREDENTIALS_FILE':str(credentials),'FIELDDESK_ARTIFACTS':str(directory)}
        worker_log = (directory/'worker.log').open('w',encoding='utf-8')
        worker = subprocess.Popen([sys.executable,str(ROOT/'index_worker.py'),'run','--backend','ollama','--no-env'],
                                  cwd=ROOT,env=environment,stdout=worker_log,stderr=subprocess.STDOUT)
        server = None
        processes, app_logs, lifecycle, controls = [], [], [], {}
        def sanitize(path):
            safe=path.read_text(encoding='utf-8')
            values=[legacy_user,legacy_operator,url,*[account['token'] for account in accounts.values()]]
            if db.postgres:
                values.append(urlsplit(url).password)
            for value in values:
                if value: safe=safe.replace(value,'[REDACTED DEMO CREDENTIAL]')
            path.write_text(safe,encoding='utf-8')
        def start(label, **fault):
            assert all(process.poll() is not None for process in processes), 'Previous owned app must exit before replacement'
            info = Path(temporary)/(label+'-info.json')
            config = Path(temporary)/(label+'-config.json')
            stop_file = Path(temporary)/(label+'-stop')
            exit_file = Path(temporary)/(label+'-exited.json')
            descriptor = os.open(config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w',encoding='utf-8') as output:
                json.dump({'database':url,'legacy_user':legacy_user,'legacy_operator':legacy_operator,
                           'info':str(info),'stop':str(stop_file),'exited':str(exit_file),**fault},output)
            path = directory/('app-'+label+'.log')
            log = path.open('w',encoding='utf-8');app_logs.append((path,log))
            instance = subprocess.Popen([sys.executable,str(ROOT/'workflow_server.py'),'--config',str(config)],
                                        cwd=ROOT,env=environment,stdout=log,stderr=subprocess.STDOUT)
            processes.append(instance)
            controls[instance.pid]=(stop_file,exit_file)
            deadline = time.monotonic()+90
            while True:
                if instance.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('Owned demo app process did not start; inspect sanitized app log')
                try:
                    details = json.loads(info.read_text(encoding='utf-8'))
                    base = details['base_url']
                    with urlopen(base+'/health',timeout=2) as response:
                        if response.status==200: break
                except (OSError,json.JSONDecodeError):
                    time.sleep(0.1)
            lifecycle.append({'stage':label,'pid':details['pid'],'launcher_pid':instance.pid,'ready':True})
            return instance,base
        def stop(instance):
            if instance and instance.poll() is None:
                stop_file,exit_file=controls[instance.pid]
                stop_file.touch()
                try: instance.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if os.name=='nt':
                        subprocess.run(['taskkill','/PID',str(instance.pid),'/T','/F'],capture_output=True,check=True)
                    else: instance.kill()
                    instance.wait(timeout=10)
                    raise RuntimeError('Owned app failed graceful process shutdown; verifier cannot pass')
                assert instance.poll() is not None
                entry=next((row for row in lifecycle if row['launcher_pid']==instance.pid),None)
                if entry is not None:
                    exited=json.loads(exit_file.read_text(encoding='utf-8'))
                    assert exited=={'pid':entry['pid'],'server_returned':True} and instance.returncode==0
                    entry.update(exit_confirmed=True,exit_code=instance.returncode)
        def browser(base,mode=None):
            env={**environment,'FIELDDESK_BASE_URL':base}
            path=directory/('browser-'+mode+'.log' if mode else 'browser.log')
            try:
                with path.open('w',encoding='utf-8') as log:
                    result=subprocess.run(['node',str(ROOT/'browser/workflow.mjs'),*(['--'+mode] if mode else [])],
                                          cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=420)
            finally:
                sanitize(path)
            if result.returncode:
                raise RuntimeError('Browser workflow failed; inspect the local browser log')
        def get(base,name,path):
            response=httpx.get(base+path,headers={'Authorization':'Bearer '+accounts[name]['token']},timeout=30)
            response.raise_for_status()
            return response.json()
        try:
            server,base=start('initial')
            browser(base)
            ui=json.loads((directory/'browser-workflow.json').read_text(encoding='utf-8'))
            answer=ui['answer']
            trace=get(base,'alice','/rag/traces/'+answer['trace_id'])
            metadata=json.loads(trace['metadata'])
            assert metadata.get('model')=='qwen3.5:9b' and metadata.get('prompt_eval_count',0)>0
            for cite in answer['citations']:
                assert cite['document_id']!=private['document_id']
                source=get(base,'alice','/rag/chunks/'+cite['chunk_id']+'?space_id='+cite['space_id'])
                assert cite['quote'] in source['text']
            restricted=knowledge.ready_chunks(private['version_id'],embedder.space.id)[0]['id']
            response=httpx.get(base+'/rag/chunks/'+restricted,params={'space_id':embedder.space.id},
                               headers={'Authorization':'Bearer '+accounts['alice']['token']})
            assert response.status_code==404
            restricted_status=response.status_code
            review_id=ui['review_id'];ticket_id=ui['approved']['action']['ticket_id']
            before={'review':get(base,'alice','/rag/reviews/'+review_id),
                    'ticket':get(base,'alice','/rag/tickets/'+ticket_id),
                    'job':get(base,'admin','/rag/jobs/'+ui['job']['id'])}
            stop(server);server=None
            with socket.socket() as unavailable:
                unavailable.bind(('127.0.0.1',0))
                server,base=start('outage',generation_endpoint='http://127.0.0.1:'+str(unavailable.getsockname()[1]))
                try: browser(base,'outage')
                finally: stop(server);server=None
            server,base=start('timeout',generation_timeout=0.001)
            try:
                response=httpx.post(base+'/rag/chat',headers={'Authorization':'Bearer '+accounts['alice']['token']},
                    json={'message':'What must I record in a projector repair ticket?'},timeout=60)
                response.raise_for_status();timeout_result=response.json()
                assert timeout_result['handoff'] and not timeout_result['citations']
            finally: stop(server);server=None
            server,base=start('restarted')
            after={'review':get(base,'alice','/rag/reviews/'+review_id),
                   'ticket':get(base,'alice','/rag/tickets/'+ticket_id),
                   'job':get(base,'admin','/rag/jobs/'+ui['job']['id'])}
            assert before==after
            assert len({row['pid'] for row in lifecycle})==4
            assert all(row.get('exit_confirmed') for row in lifecycle[:-1])
            for path in ['/rag/reviews/'+review_id,'/rag/tickets/'+ticket_id]:
                assert httpx.get(base+path,headers={'Authorization':'Bearer '+accounts['bob']['token']}).status_code==404
            browser(base,'resume')
            resumed=json.loads((directory/'browser-resume.json').read_text(encoding='utf-8'))
            assert resumed['review_id']==review_id and resumed['ticket_id']==ticket_id
            resumed_metadata=json.loads(get(base,'alice','/rag/traces/'+resumed['answer']['trace_id'])['metadata'])
            assert resumed_metadata.get('model')=='qwen3.5:9b' and resumed_metadata.get('prompt_eval_count',0)>0
            with db.connect() as conn:
                count=db.run(conn,'SELECT count(*) AS n FROM tickets').fetchone()['n']
                assert count==1
            return {'inference':'Actual local Ollama embeddings and Qwen generation; actual Chromium UI and HTTP; no mocked model/API adapters',
                    'storage':'PostgreSQL/pgvector' if db.postgres else 'SQLite exact scan (not pgvector)',
                    'platform':platform.system(),'python':platform.python_version(),
                    'embedding':asdict(embedder.space),
                    'generation':{'model':generator.model,'digest':generator.digest},
                    'answer_trace_metadata':metadata,'browser_workflow':ui,
                    'outage':json.loads((directory/'browser-outage.json').read_text()),
                    'actual_1ms_transport_timeout':timeout_result,
                    'app_restart_preserved_exact_review_ticket_job':True,'persisted_ticket_count':count,
                    'app_restart_method':'Owned OS app processes receive a private shutdown signal and are waited for before each fresh subprocess; exact HTTP review/ticket/job state reread from the same database. Final process performs a new actual model answer through Chromium. Index-worker crash/restart is a separate drill.',
                    'app_process_lifecycle':lifecycle,'post_restart_browser':resumed,
                    'post_restart_trace_metadata':resumed_metadata,
                    'restricted_source_http_status':restricted_status,
                    'limits':'Synthetic workflow demonstration, not held-out answer accuracy or production ticket-provider integration. Native SQLite evidence does not prove PostgreSQL; Linux CI runs the pgvector variant.'}
        finally:
            try:
                for process in processes: stop(process)
            finally:
                worker.terminate()
                worker.wait(timeout=20)
                worker_log.close()
                sanitize(directory/'worker.log')
                for path,log in app_logs:
                    log.close();sanitize(path)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--artifacts',type=Path,default=Path('artifacts/workflow'))
    args=parser.parse_args()
    directory=args.artifacts.resolve()
    directory.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='fielddesk-workflow-') as temporary:
        context=disposable_postgres() if args.postgres else nullcontext(str(Path(temporary)/'demo.db'))
        with context as url:
            report=demonstrate(url,directory)
    (directory/'workflow.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'passed','storage':report['storage'],'ticket_count':report['persisted_ticket_count'],
                      'app_restart':True,'report':str(directory/'workflow.json')}))
    return 0
if __name__=='__main__':
    raise SystemExit(main())
