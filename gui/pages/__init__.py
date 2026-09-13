"""桌面版界面页面：每个标签页对应一个 py 文件"""

from gui.pages.backtest_page import BacktestPage
from gui.pages.batch_page import BatchPage
from gui.pages.data_page import DataPage
from gui.pages.optimize_page import OptimizePage
from gui.pages.settings_page import SettingsPage

__all__ = ["BacktestPage", "OptimizePage", "BatchPage", "DataPage", "SettingsPage"]
