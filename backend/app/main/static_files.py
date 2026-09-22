from starlette.responses import Response
from starlette.staticfiles import StaticFiles


class SafeStaticFiles(StaticFiles):
    async def check_config(self) -> None:
        return None

    async def get_response(self, path: str, scope):
        if not self.directory:
            return Response(status_code=404)

        try:
            return await super().get_response(path, scope)
        except (FileNotFoundError, RuntimeError):
            return Response(status_code=404)
