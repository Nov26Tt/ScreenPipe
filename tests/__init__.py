"""包初始化。

让 `from capture import Capture` 这类导入在pytest 下也能工作
（pytest 会把根目录加入 sys.path，但显式声明更稳妥）。
"""
