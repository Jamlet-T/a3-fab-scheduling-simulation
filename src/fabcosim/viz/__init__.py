"""仿真结果可视化平台：把已验收的分析产物适配成演示用视图模型。

只读原始 ``analysis.json``，不改写、不重新计算模型。
"""

from .datasets import DATASETS, build_index, load_dataset

__all__ = ["DATASETS", "build_index", "load_dataset"]
