"""Math helpers for AutoResearch."""


def add(a, b):
    """Return the sum of two values."""
    return a + b


def weighted_average(values, weights):
    """Return the weighted average for matching values and weights."""
    values = list(values)
    weights = list(weights)

    if not values:
        raise ValueError("weighted_average requires at least one value")
    if len(values) != len(weights):
        raise ValueError("values and weights must have the same length")

    total_weight = sum(weights)
    if total_weight == 0:
        raise ValueError("total weight must not be zero")

    weighted_sum = sum(value * weight for value, weight in zip(values, weights))
    return weighted_sum / total_weight
