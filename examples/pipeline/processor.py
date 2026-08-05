import reader


def filter_above(readings, threshold):
    return [r for r in readings if r >= threshold]


def summarize(readings):
    return {"count": len(readings), "total": sum(readings)}


def write_output(path, summary):
    with open(path, "w") as f:
        f.write(f"count={summary['count']}\ntotal={summary['total']}\n")


def run_pipeline(threshold_path, readings_path, output_path):
    threshold = reader.load_threshold(threshold_path)
    readings = reader.load_readings(readings_path)
    kept = filter_above(readings, threshold)
    summary = summarize(kept)
    write_output(output_path, summary)
    return summary
