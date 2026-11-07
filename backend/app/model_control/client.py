import asyncio
import json


class WorkerUnavailable(Exception):
    pass


class ModelControlClient:
    def __init__(self, path: str):
        self.path = path

    async def request(self, operation: str, **arguments):
        data = json.dumps({"operation": operation, **arguments}, separators=(",", ":")).encode() + b"\n"
        if len(data) > 4096:
            raise WorkerUnavailable()
        writer = None
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.path, limit=65537), 5)
            writer.write(data)
            await asyncio.wait_for(writer.drain(), 5)
            raw = await asyncio.wait_for(reader.readline(), 10)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise WorkerUnavailable()
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise WorkerUnavailable()
            return response
        except (OSError, asyncio.TimeoutError, ValueError, UnicodeError) as exc:
            raise WorkerUnavailable() from exc
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
