from concurrent.futures import ThreadPoolExecutor

from app import metrics


def test_vl_trace_collects_parallel_model_calls():
    trace_id = metrics.start_vl_trace()

    def model_call(index):
        metrics.add_vl_usage(
            {"prompt_tokens": 100 + index, "completion_tokens": 10, "total_tokens": 110 + index},
            model="vision-model", provider="provider-a",
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(metrics.run_with_vl_trace, trace_id, model_call, index)
                   for index in range(3)]
        for future in futures:
            future.result()

    usage = metrics.finish_vl_trace(trace_id)
    assert usage["calls"] == 3
    assert usage["prompt_tokens"] == 303
    assert usage["completion_tokens"] == 30
    assert usage["total_tokens"] == 333
    assert [detail["call"] for detail in usage["details"]] == [1, 2, 3]
