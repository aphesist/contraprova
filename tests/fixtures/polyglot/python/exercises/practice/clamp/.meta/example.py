def clamp(value, low, high):
    if low > high:
        raise ValueError("low > high")
    return max(low, min(high, value))
