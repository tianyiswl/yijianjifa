"""UI-independent application contracts; native transport adapters remain separate."""
from .task_runtime import TaskRuntime
from .desktop_api import DesktopApi
from .media_access import MediaAccess
from .dispatch_worker import DispatchWorker
from .excel_access import ExcelAccess
