from .base import BaseOCREngine
from .local_import import LocalImportEngine
from .remote_api import RemoteAPIEngine
from .cli_wrapper import CLIEngine

def get_engine(engine_type, config=None):
    """
    Factory to create OCR engine instance.
    :param engine_type: 'local', 'remote', or 'cli'
    :param config: Global configuration dict (containing api tokens etc)
    """
    if engine_type == 'local':
        return LocalImportEngine(config)
    elif engine_type == 'cli':
        return CLIEngine(config)
    else:
        # Default to remote
        return RemoteAPIEngine(config)
