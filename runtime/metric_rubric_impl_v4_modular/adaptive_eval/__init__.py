"""Task-specific rubric instantiation and uniform binary metric evaluation."""

__version__ = "4.1.0"

from .instantiation import instantiate
from .unified import evaluate_metrics

evaluate = evaluate_metrics
__all__ = ["instantiate", "evaluate_metrics", "evaluate"]

