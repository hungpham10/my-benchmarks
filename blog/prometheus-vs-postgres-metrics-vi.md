---
title: "Prometheus vs PostgreSQL: Một Câu Hỏi Phỏng Vấn, Được Benchmark"
date: 2026-10-18
lastmod: 2026-10-18
tags: [prometheus, postgresql, benchmark, metrics, time-series]
excerpt: "Một Head of Platform từng hỏi tại sao Prometheus hay InfluxDB lại tốt hơn PostgreSQL cho metrics. Tôi trả lời 'PostgreSQL cũng làm được.' Đó là cái bẫy. Tôi đã benchmark cả ba trên cùng dữ liệu. Dưới đây là số đo thực."
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "Prometheus vs PostgreSQL: Một Câu Hỏi Phỏng Vấn, Được Benchmark",
  "description": "Một Head of Platform từng hỏi tại sao Prometheus hay InfluxDB lại tốt hơn PostgreSQL cho metrics. Tôi đã benchmark cả ba trên cùng dữ liệu. Dưới đây là số đo thực.",
  "author": {
    "@type": "Person",
    "name": "Platform Engineering Team"
  },
  "publisher": {
    "@type": "Organization",
    "name": "Platform Engineering Blog"
  },
  "datePublished": "2026-10-18",
  "dateModified": "2026-10-18",
  "mainEntityOfPage": {
    "@type": "WebPage",
    "@id": "https://example.com/blog/prometheus-vs-postgres-metrics-vi"
  },
  "keywords": ["prometheus", "postgresql", "benchmark", "metrics", "time-series"],
  "articleSection": "Engineering"
}
</script>

<meta property="og:title" content="Prometheus vs PostgreSQL: Một Câu Hỏi Phỏng Vấn, Được Benchmark">
<meta property="og:description" content="Một Head of Platform từng hỏi tại sao Prometheus hay InfluxDB lại tốt hơn PostgreSQL cho metrics. Tôi đã benchmark cả ba trên cùng dữ liệu. Dưới đây là số đo thực.">
<meta property="og:type" content="article">
<meta property="og:article:published_time" content="2026-10-18">
<meta property="twitter:card" content="summary">

# Prometheus vs PostgreSQL: Một Câu Hỏi Phỏng Vấn, Được Benchmark

## Câu hỏi tôi từng trả lời sai

Trong một buổi phỏng vấn vị trí Platform Engineering tại một ngân hàng tiêu dùng (tên công ty được giấu tên), Head of Platform hỏi tôi một câu nghe có vẻ đơn giản:

> "Tại sao phải dùng Prometheus hay InfluxDB cho metrics, thay vì PostgreSQL?"

Tôi đã vào cái bẫy. Tôi trả lời PostgreSQL cũng làm được — bạn có thể lưu metrics vào bảng Postgres và truy vấn bằng SQL.

Anh ấy gật đầu. Chỉ sau đó tôi mới nhận ra câu hỏi thực sự: không phải "PostgreSQL có thể lưu metrics không", mà là "bạn biết giá trả cho sự tiện lợi đó là gì chưa?" Tôi không có câu trả lời tốt.

Sau buổi phỏng vấn, tôi đã benchmark cả ba hệ thống trên cùng dữ liệu, cùng tải truy vấn, cùng khung tài nguyên. Bài viết này là câu trả lời tôi muốn đưa anh ấy — không phải ý kiến, là số đo thực.

## Key Takeaways

- **Disk:** Prometheus **12.68 bytes/sample** vs PostgreSQL chuẩn hóa **540.09 B/sample** vs PostgreSQL JSONB **1,024.56 B/sample**. Prometheus nhỏ hơn 43× so với Postgres chuẩn hóa và 81× so với JSONB.
- **Ghi:** Prometheus bỏ 0.5 s ghi thật plus ~72 s pacing delay (chắn chống WAL backpressure, không phải nút điều chỉnh). PostgreSQL COPY: 0.6 s (chuẩn hóa) / 1.4 s (JSONB).
- **Điểm phá vỡ** (k6 load test, **một hình dạng truy vấn duy nhất**: `sum(rate(http_requests_total[5m]))`, ramp 10→200→2000 req/s, ngưỡng p95 500 ms): PostgreSQL chuẩn hóa hoàn thành 469,604 iterations tại p50 0.5 ms; Prometheus hoàn thành 176,266 tại p50 1.4 s; PostgreSQL JSONB chỉ hoàn thành 278 tại p50 66 s.
- **Năm hình dạng truy vấn đã định nghĩa:** q1–q5 được nêu trong `benchctl/shared/queries.py` với PromQL và SQL tương đương; q1 được đo qua k6, q2–q5 giờ có thể đo với phase query-sweep mới.
- **Parity:** số lượng mẫu khớp trong 0.6% (−800/144,000, trong ngưỡng 2%). Tổng chênh ~3× và chỉ ghi làm chẩn đoán.
- **Caveat:** Prometheus chưa compact (cửa sổ 45 phút dưới ngưỡng compact ~3 h head span), CPU/RAM chưa ghi trong CI.

## Prometheus thực sự đã làm gì: bốn việc "từ chối"

Prometheus không thắng vì có thuật toán nén tốt hơn. Nó thắng vì nó **từ chối những gì PostgreSQL làm**. Mỗi việc từ chối là một chi tiết kiến trúc có chi phí có thể đo:

| # | Prometheus từ chối | Hệ quả kiến trúc |
|---|-------------------|-----------------|
| 1 | `UPDATE` / `DELETE` tại chỗ | Chỉ ghi append-only, time-ordered |
| 2 | Một hàng mỗi sample | Label set *là* identity; không cần lặp nhãn trên mỗi dòng |
| 3 | Chỉ mục B-tree đa mục đích | Chỉ mục inverted postings (series refs, không phải rows) |
| 4 | Cột hiển thị MVCC | Không 23–24 B tuple header/row, không WAL cho từng sample |

Hai điểm cuối chịu phần lớn khoản cách biệt bytes/sample dưới đây.

## Cùng dữ liệu, cùng tải

Tất cả ba hệ thống nhận cùng tập dữ liệu tổng hợp (~20k chuỗi, scrape 15s, cửa sổ 45 phút). Mỗi hệ thống chạy dưới cùng ràng buộc: **1.5 CPU / 4 GB RAM**. Mọi chạy đều verifies *sample parity* trong khoảng 2% (`failed: 0`). Tổng chỉ ghi làm chẩn đoán, không phải claim được xác thực.

## Dung lượng đĩa (chart + bảng)

| Hệ thống | Mẫu được giữ lại | Bytes trên đĩa | **Bytes / mẫu** |
|--------|------------------|----------------|-----------------|
| Prometheus TSDB | 143,200 | 1.7 MB | **12.68** |
| PostgreSQL Chuẩn hóa | 144,000 | 74.2 MB | **540.09** |
| PostgreSQL JSONB | 144,000 | 140.7 MB | **1,024.56** |

<svg role="img" aria-labelledby="chart-disk-title chart-disk-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-disk-title">Dung lượng đĩa — bytes trên mỗi mẫu</title>
  <desc id="chart-disk-desc">Biểu đồ cột ngang so sánh bytes trên mẫu: Prometheus 12.68, PostgreSQL chuẩn hóa 540.09, PostgreSQL JSONB 1,024.56.</desc>
  <rect x="80" y="30" width="11" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="33" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="64" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres chuẩn hóa</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres JSONB</text>
  <text x="93" y="59" font-family="system-ui, sans-serif" font-size="12" fill="#374151">12.68</text>
  <text x="95" y="119" font-family="system-ui, sans-serif" font-size="12" fill="#374151">540.09</text>
  <text x="95" y="179" font-family="system-ui, sans-serif" font-size="12" fill="#374151">1,024.56</text>
</svg>

### Tại sao PostgreSQL mất nhiều chỗ?

Chi phí không nằm ở cấu trúc chỉ mục — nó nằm ở **văn bản nhãn**.

- **PostgreSQL JSONB** lưu toàn bộ tài liệu nhãn trên *mỗi dòng*. Dù `env="production"` lặp lại hàng triệu lần, PostgreSQL không có string interning cấp hệ thống. Chỉ mục GIN báo cho Postgres xem dòng nào khớp, nhưng mỗi khớp vẫn phải quay về heap để đọc giá trị → một match luôn đi kèm heap fetch.
- **PostgreSQL Chuẩn hóa** ngược lại: hàng rẻ, chỉ mục đắt (tỉ lệ index/heap ~0.61), nhưng vẫn phải lưu văn bản nhãn ít nhất một lần mỗi dòng.
- **Prometheus** lưu nhãn một lần duy nhất trong inverted index (postings list). Sample sau này chỉ là hai số: timestamp + giá trị.

Phép nén quan trọng nhất diễn ra ngay khi sample đầu tiên vào cỗ máy: **delta-of-delta** trên timestamps và **Gorilla XOR** trên giá trị. Khi tần suất scrape ổn định, khoảng cách giữa các mốc thời gian gần bằng không, nên timestamps tốn khoảng 1 bit mỗi. Một chunk 120 sample chỉ tốn khoảng 15 bytes cho timestamps — không phải vì compaction cải thiện nén, mà vì Prometheus ghi theo thứ tự thời gian ngay từ đầu.

## Tốc độ ghi: thực tế, không phải wall-clock

| Hệ thống | Thời gian ghi thực | Tốc độ khi ghi |
|----------|-------------------|----------------|
| PostgreSQL JSONB | 1.4 s (COPY) | ~106k mẫu/s |
| PostgreSQL Chuẩn hóa | 0.6 s (COPY) | ~254k mẫu/s |
| Prometheus | 0.5 s ingest + 72 s pacing delay | ~284k mẫu/s khi ghi |

Cảnh báo quan trọng: thời gian wall của Prometheus chứa ~72 giây **pacing delay** (`REQUEST_DELAY`) để tránh quá tải WAL. Bỏ pacing thì Prometheus mất khoảng 30% mẫu. Đây là *chắn chống áp lực ngược*, không phải nút điều chỉnh — so sánh wall-clock trực tiếp là không công bằng. Điều quan trọng là throughput thực khi ghi sau khi tính pacing, và ở đó Prometheus giữ vững được.

Lưu ý: 72 giây này là của **Prometheus remote-write**, không phải PostgreSQL COPY. Postgres chạy COPY unpaced, nên con số trên bảng là thật cho mỗi hệ thống.

## Truy vấn tải: ai gãy trước?

Tôi dùng k6 để ramp tốc độ truy vấn từ 10 lên 2,000 requests/giây (`ramping-arrival-rate`, mô hình mở), với ngưỡng p95 500 ms. Tất cả ba hệ thống đều đến điểm phá vỡ (k6 exit code 99).

**Quan trọng: k6 chỉ thử đúng một hình dạng truy vấn — q1 counter rate:**

```
sum(rate(http_requests_total[5m]))
```

Đây là PromQL cho "counter rate trên tất cả chuỗi counter." PostgreSQL trả lời hình dạng tương tự với `lag() OVER (PARTITION BY ...)` qua cửa sổ thời gian. Dưới đây là mỗi hệ thống làm gì ngay trước khi gãy:

| Hệ thống | Iterations khi gãy | p50 latency | p95 latency | Iterations bị từ chối |
|----------|-------------------|-------------|-------------|----------------------|
| PostgreSQL JSONB | 278 | 66 s | 81 s | 469,522 |
| PostgreSQL Chuẩn hóa | 469,604 | 0.5 ms | 0.7 ms | 196 |
| Prometheus | 176,266 | 1.4 s | 3.1 s | 293,533 |

<svg role="img" aria-labelledby="chart-break-title chart-break-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-break-title">Điểm phá vỡ — iterations đạt trước khi p95 > 500 ms</title>
  <desc id="chart-break-desc">Biểu đồ cột ngang: PostgreSQL chuẩn hóa 469,604 iterations, Prometheus 176,266 iterations, PostgreSQL JSONB 278 iterations.</desc>
  <rect x="80" y="30" width="64" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="90" width="24" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="150" width="1" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres chuẩn hóa</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Prometheus</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres JSONB</text>
  <text x="95" y="59" font-family="system-ui, sans-serif" font-size="12" fill="#374151">469,604</text>
  <text x="95" y="119" font-family="system-ui, sans-serif" font-size="12" fill="#374151">176,266</text>
  <text x="95" y="179" font-family="system-ui, sans-serif" font-size="12" fill="#374151">278</text>
</svg>

**Kết quả chính:** Ở hình dạng truy vấn này (tính `rate()` qua chuỗi counter, nhóm theo `job`), PostgreSQL Chuẩn hóa thắng cả độ trễ lẫn throughput. Prometheus từ chối gần 293k iterations vì mỗi truy vấn mất khoảng 1–3 giây — nó không kịp ở 2,000 req/s. JSONB gãy gần như ngay lập tức vì mỗi truy vấn mất khoảng 80 giây (JSONB containment + heap fetch cho mọi dòng).

Lưu ý thêm: độ trễ p95 của Prometheus chênh lệch giữa các lần chạy (~1.9–3.1s). Độ trễ tại điểm phá vỡ không ổn định bằng giá trị trung bình — coi nó là một khoảng.

## Năm kịch bản truy vấn — đã đo vs đã định nghĩa

Benchmark định nghĩa **năm hình dạng truy vấn** trong `benchctl/shared/queries.py`, mỗi cái có phiên bản PromQL và SQL tương đương cho cả hai schema PostgreSQL. Chỉ **q1** được thực thi qua k6 load test trong run `gh-219.1`. Phase query-sweep (đã thêm vào `benchctl/driver/run.py`) giờ đo độ trễ theo từng truy vấn cho tất cả năm hình dạng; kết quả từ phase đó sẽ báo cáo trong run sau.

| # | Kịch bản | PromQL | SQL tương đương | Đã đo trong gh-219.1 |
|---|----------|--------|-----------------|----------------------|
| q1 | Counter rate trên mọi chuỗi counter | `sum(rate(http_requests_total[5m]))` | `lag() OVER (PARTITION BY ...)` qua cửa sổ 5m | **Có** — k6 breaking point |
| q2 | Aggregation nhóm theo job | `sum by (job) (queue_depth)` | `GROUP BY job` với `avg(value)` | Không — đã định nghĩa, chưa đo |
| q3 | Per-series p90 qua 1 giờ | `quantile_over_time(0.9, queue_depth{job="job-00"}[1h])` | `percentile_cont(0.9) WITHIN GROUP` | Không — đã định nghĩa, chưa đo |
| q4 | Selective wide: một job qua toàn window | `sum(queue_depth{job="job-00"})` | `WHERE s.job = 'job-00'` + `avg(value)` | Không — đã định nghĩa, chưa đo |
| q5 | Short range: một chuỗi duy nhất, 15 phút | `queue_depth{instance="host-0000"}` | `WHERE s.instance = 'host-0000'` + `avg(value)` | Không — đã định nghĩa, chưa đo |

**Kỳ vọng winner** (từ mục đích kiến trúc, không phải đo trong gh-219.1): q1 → Prometheus; q2 → ngang nhau; q3 → PostgreSQL; q4 → Prometheus; q5 → PostgreSQL. Đây là giả định để xác thực qua query-sweep phase, không phải claim từ run này.

## PostgreSQL thắng gì — và tôi đã sai chỗ nào

Nếu bài viết chỉ nói "PostgreSQL chậm", thì nó sai. Kiến trúc cho thấy PostgreSQL thực sự vượt trội ở ba điểm:

1. **Percentiles qua khoảng thời gian.** `percentile_cont(0.9) WITHIN GROUP (ORDER BY value)` sắp xếp trong cơ sở dữ liệu, không bị trần cấp `--query.max-samples` của Prometheus (mặc định 50M). `quantile_over_time` của Prometheus sắp xếp trong RAM và bị trần cấp.
2. **Tra cứu điểm một chuỗi duy nhất.** Lấy một chuỗi tại một mốc thời gian là dưới 1 ms trong PostgreSQL nhờ B-tree / BRIN point lookup.
3. **SQL phức tạp và joins.** PromQL là ngôn ngữ kín. PostgreSQL cho phép bạn join metrics trực tiếp với dữ liệu nghiệp vụ (khách hàng, giao dịch) trong cùng một câu truy vấn.

## Honesty caveats — câu trả lời tôi muốn anh ấy nghe nhất

1. **Sum parity không được xác thực.** Tổng Prometheus (154M) vs PostgreSQL (452M) chênh khoảng 3×. Tôi cố tình không claim sum parity; số lượng *mẫu* khớp trong 0.6% (−800 trong 144,000). Khoảng cách tổng chưa được giải thích và ghi làm chẩn đoán.
2. **Prometheus mất 0.6% mẫu** (143,200 vs 144,000). Đã ghi lại, không giấu.
3. **Chưa compact.** Cửa sổ 45 phút dưới ngưỡng compact của Prometheus (~3 h head span), nên con số đĩa phản ánh head chunks, không phải steady-state on-disk blocks.
4. **CPU/RAM không có trong báo cáo.** Môi trường CI không thể hiển thị chỉ số cAdvisor container cho runner; chỉ số này gửi lên Grafana Cloud nhưng không công bố trong bài.
5. **Chỉ q1 được đo dưới tải.** Kế legs k6 chỉ thử `sum(rate(http_requests_total[5m]))`. q2–q5 đã được định nghĩa trong query catalog với PromQL + SQL tương đương, và query-sweep phase có thể đo chúng trong run sau.
6. **Một số con số là literature, không phải đo.** Con số ~1.37 B/sample của Gorilla và claim nén 90–95% của TimescaleDB đến từ papers và documentation, không phải từ benchmark này.

## Câu trả lời tôi muốn đưa anh ấy

Nếu gặp lại Head of Platform, tôi sẽ không nói "PostgreSQL cũng làm được". Tôi sẽ nói:

> *"PostgreSQL có thể lưu metrics, nhưng bạn phải trả giá: tuple header, index bloat, WAL cho từng sample, và heap fetch mỗi khi bạn hỏi 'giá trị của nhãn này là gì'. Prometheus từ chối những chi phí đó — không UPDATE tại chỗ, không DELETE chọn lọc, không one-row-per-sample. Đổi lấy nó nhận được cấu trúc chỉ đọc, tuần tự, nén tốt, decompression gần như miễn phí. Giá trả là sự cứng nhắc vận hành."*

| Tình huống | Chọn |
|------------|------|
| Giám sát hạ tầng, Kubernetes, cảnh báo tức thời | **Prometheus** |
| Dữ liệu time-series nhiều năm, JOIN với dữ liệu nghiệp vụ | **PostgreSQL + TimescaleDB** (hoặc hybrid) |
| Percentile SQL không ceiling, ad-hoc reporting | **PostgreSQL** |
| High cardinality, long retention, chi phí đĩa quan trọng | **Prometheus** |

## Reproduce

Toàn bộ benchmark chạy tự động trên GitHub Actions tại `github.com/hungpham10/my-benchmarks` (thư mục `benchctl/`). Số trong bài là output của run `gh-219.1` trên nhánh `main`. Bạn có thể chạy `scale=smoke` (~3 phút) để tự thấy parity check, rồi `scale=pg` (~31 phút) để đi sâu. Phase query-sweep (độ trễ theo từng truy vấn q1–q5) là một phần của pipeline `scale=pg`.

---

*Tất cả số liệu từ một lần chạy GitHub Actions duy nhất (scale=pg, run `gh-219.1`), Prometheus 3.15.0 vs PostgreSQL 18.6, mỗi hệ giới hạn 1.5 CPU / 4 GB. Tên công ty và Head of Platform đã được giấu tên theo yêu cầu.*