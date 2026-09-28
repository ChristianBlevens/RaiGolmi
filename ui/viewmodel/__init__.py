"""The shared selector view-model.

The layer-shell selector is a thin layer over this, so its logic is tested without a screen
and **the selector never computes compatibility itself**. It asks raigolmid; this object holds the answer.
"""
from .model import ROWS, InstanceBadge, Row, RowItem, SelectorModel

__all__ = ["ROWS", "InstanceBadge", "Row", "RowItem", "SelectorModel"]
