// Copyright (C) 2023-2024 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

#include "openvino/genai/perf_metrics.hpp"
#include "openvino/openvino.hpp"
#include <tuple>
#include <numeric>
#include <cmath>

namespace {

ov::genai::MeanStdPair calc_mean_and_std(const std::vector<ov::genai::MicroSeconds>& durations) {
    // Accepts time durations in microseconds and returns standard deviation and mean in milliseconds.
    float mean = std::accumulate(durations.begin(), durations.end(), 0.0f, 
        [](const float& acc, const ov::genai::MicroSeconds& duration) -> float {
            return acc + duration.count() / 1000.0f;
        });
    mean /= durations.size();
    
    float sum_square_durations = std::accumulate(durations.begin(), durations.end(), 0.0f,
        [](const float& acc, const ov::genai::MicroSeconds& duration) -> float {
            auto d = duration.count() / 1000.0f;
            return acc + d * d;
        });
    float std = std::sqrt(sum_square_durations / durations.size() - mean * mean);
    return {mean, std};
}


} // namespace

namespace ov {
namespace genai {

float PerfMetrics::get_microsec(std::chrono::steady_clock::duration duration) {
    return std::chrono::duration_cast<std::chrono::microseconds>(duration).count();
}
    
void PerfMetrics::evaluate_statistics(std::optional<TimePoint> start_time) {
    // If start_tiem is specified then recalcualte durations according to start times and calculate statistics only after that.
    if (start_time.has_value()) {
        auto start_time_val = *start_time;
        auto& tok_times = raw_metrics.m_new_token_times;
        auto& batch_sizes = raw_metrics.m_batch_sizes;
        raw_metrics.m_durations = std::vector<MicroSeconds>(tok_times.size());

        auto ttft = tok_times[0] - start_time_val;
        raw_metrics.m_times_to_first_token = std::vector<MicroSeconds>();
        raw_metrics.m_times_to_first_token.emplace_back(ttft);
        num_generated_tokens = 0;
        for (size_t i = 0; i < tok_times.size(); ++i) {
            raw_metrics.m_durations[i] = tok_times[i] - start_time_val;
            
            // If in 10 ms a batch of 5 new tokens is generated then TPOT is 10 / 5 = 2 tok/ms.
            raw_metrics.m_durations[i] /= batch_sizes[i];
            num_generated_tokens += batch_sizes[i];
            start_time_val = tok_times[i];
        }
    }
    
    // calc_mean_and_std will convert microsecond to milliseconds.
    tpot = calc_mean_and_std(raw_metrics.m_durations);
    ttft = calc_mean_and_std(raw_metrics.m_times_to_first_token);

    generate_duration = calc_mean_and_std(raw_metrics.generate_durations);
    generate_duration = calc_mean_and_std(raw_metrics.generate_durations);

    tokenization_duration = calc_mean_and_std(raw_metrics.tokenization_durations);
    detokenization_duration = calc_mean_and_std(raw_metrics.detokenization_durations);    
    
    // tokens per second
    throughput = {1000.0f / tpot.mean, (tpot.std * 1000.0f) / (tpot.mean * tpot.mean)};
}

PerfMetrics PerfMetrics::operator+(const PerfMetrics& right) const {
    OPENVINO_ASSERT(right.load_time == load_time, "generation metrics can be accumulated only for the same pipeline");
    
    // Copy left value to res.
    PerfMetrics res = *this;

    // Concatenate durations, batch_sizes first token times.
    auto& new_durations = res.raw_metrics.m_durations;
    auto& new_batch_sizes = res.raw_metrics.m_batch_sizes;
    auto& new_times_to_first_token = res.raw_metrics.m_times_to_first_token;
    auto& right_durations = right.raw_metrics.m_durations;
    auto& right_batch_sizes = right.raw_metrics.m_batch_sizes;
    auto& right_times_to_first_token = right.raw_metrics.m_times_to_first_token;
    
    new_durations.insert(new_durations.end(), right_durations.begin(), right_durations.end());
    new_times_to_first_token.insert(new_times_to_first_token.end(), right_times_to_first_token.begin(), right_times_to_first_token.end());
    new_batch_sizes.insert(new_batch_sizes.end(), right_batch_sizes.begin(), right_batch_sizes.end());

    // Concatenate tokenization/detokenization and total generation times.
    auto& new_tok_durations = res.raw_metrics.tokenization_durations;
    auto& new_detok_durations = res.raw_metrics.detokenization_durations;
    auto& new_gen_durations = res.raw_metrics.generate_durations;
    auto& right_tok_durations = right.raw_metrics.tokenization_durations;
    auto& right_detok_durations = right.raw_metrics.detokenization_durations;
    auto& right_gen_durations = right.raw_metrics.generate_durations;
    
    new_tok_durations.insert(new_tok_durations.end(), right_tok_durations.begin(), right_tok_durations.end());
    new_detok_durations.insert(new_detok_durations.end(), right_detok_durations.begin(), right_detok_durations.end());
    new_gen_durations.insert(new_gen_durations.end(), right_gen_durations.begin(), right_gen_durations.end());

    res.num_generated_tokens = num_generated_tokens + right.num_generated_tokens;
    res.num_input_tokens = num_generated_tokens + right.num_input_tokens;
    res.load_time = load_time;
    res.evaluate_statistics();
    return res;
}

PerfMetrics& PerfMetrics::operator+=(const PerfMetrics& right) {
    *this = *this + right;
    return *this;
}

} // namespace genai
} // namespace ov
