def load_threshold(path):
    with open(path) as f:
        return float(f.read().strip())


def load_readings(path):
    with open(path) as f:
        return [float(line) for line in f if line.strip()]
