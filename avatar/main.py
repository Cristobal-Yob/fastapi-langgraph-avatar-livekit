import asyncio
import contextlib
import logging
import os
import uvicorn
from .config import load_settings
from .service import Trial
from .web import create_app


async def run():
    logging.disable(logging.CRITICAL)
    trial = Trial(load_settings())
    # Only known owned cleanup may resume. Creates never replay after a crash.
    if trial.spent:
        try:
            async with asyncio.timeout(18): await trial.stop()
        except TimeoutError: pass
    server = uvicorn.Server(uvicorn.Config(create_app(trial),host='0.0.0.0',port=int(os.environ.get('PORT','8000')),
        access_log=False,log_config=None,server_header=False,timeout_graceful_shutdown=20))
    watcher = asyncio.create_task(trial.watchdog())
    try: await server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError): await watcher
        async with asyncio.timeout(20): await trial.close()


if __name__ == '__main__': asyncio.run(run())
