def load_data():
    return {"a": 1, "b": 2, "c": 3}


def process(data):
    return {k: v * 2 for k, v in data.items()}


def summarize(data):
    return sum(data.values())


def main():
    data = load_data()
    processed = process(data)
    return summarize(processed)
