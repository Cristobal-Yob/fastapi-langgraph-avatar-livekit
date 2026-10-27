"""One dyno, supervised included core and optional public adapter."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def commands(variant, port):
    core = [sys.executable, '-B', '-m', 'cloud.serve']
    if variant == 'chainlit':
        return [(core, '18080'), ([sys.executable,'-m','chainlit','run','cloud/chainlit_app.py',
            '--host','0.0.0.0','--port',port,'--headless'], None)]
    if variant == 'openwebui':
        return [(core, '18080'), ([sys.executable,'-B','-m','cloud.webui_entry','serve','--host','0.0.0.0','--port',port], None)]
    if variant.startswith('whatsapp-'):
        return [(core, '18080'), (['node','src/main.mjs'], None)]
    if variant == 'avatar-livekit':
        return [(core, '18080'), ([sys.executable,'-B','-m','avatar.main'], None)]
    return [(core, port)]


def validate(variant):
    from cloud.security import public_origin, validate_token
    validate_token(os.environ.get('AGENT_API_TOKEN',''))
    if variant != 'base': public_origin()
    if variant == 'chainlit':
        validate_token(os.environ.get('CHAINLIT_AUTH_SECRET',''))
        if len(os.environ.get('CHAINLIT_PASSWORD_HASH','')) != 64 or len(os.environ.get('CHAINLIT_PASSWORD_SALT','')) < 16:
            raise ValueError('chainlit_password_configuration_required')
    if variant == 'openwebui':
        validate_token(os.environ.get('WEBUI_SECRET_KEY',''))
        if not os.environ.get('DATABASE_URL','').startswith(('postgres://','postgresql://')):
            raise ValueError('openwebui_durable_database_required')
        os.environ.update(ENABLE_SIGNUP='false', WEBUI_AUTH='true', ENABLE_PERSISTENT_CONFIG='true',
                          ENABLE_OPENAI_API='false', ENABLE_OLLAMA_API='false')
        if not os.environ.get('WEBUI_ADMIN_EMAIL') or len(os.environ.get('WEBUI_ADMIN_PASSWORD','')) < 20:
            raise ValueError('explicit_admin_bootstrap_required')


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int)
    args = parser.parse_args()
    if args.port: os.environ['PORT'] = str(args.port)
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    from dotenv import load_dotenv
    load_dotenv(root/'.env', override=False)
    variant = json.loads((root/'variant.json').read_text())['variant']
    os.environ.update(APP_VARIANT=variant, PYTHONDONTWRITEBYTECODE='1',
        LANGSMITH_TRACING='false', LANGCHAIN_TRACING_V2='false', ANONYMIZED_TELEMETRY='false',
        DO_NOT_TRACK='true', SCARF_NO_ANALYTICS='true')
    os.environ['PYTHONPATH'] = str(root)
    children, stopping = [], False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    code = 0
    try:
        validate(variant)
        for command, bind in commands(variant, os.environ.get('PORT','8000')):
            env = dict(os.environ)
            if bind: env['CORE_PORT'] = bind
            # Suppress third-party process output: no content, QR, credentials or provider errors in logs.
            children.append(subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        while not stopping:
            if any(child.poll() is not None for child in children):
                code = 1
                break
            time.sleep(0.2)
    except Exception:
        print('Startup refused. Check required configuration; no automatic retry.')
        code = 1
    finally:
        for child in children:
            if child.poll() is None: child.terminate()
        deadline = time.monotonic()+24
        for child in children:
            try: child.wait(timeout=max(0.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired: child.kill(); child.wait()
    return code


if __name__ == '__main__': raise SystemExit(main())
