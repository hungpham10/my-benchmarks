---
title: "Tại sao không dùng PostgreSQL cho metrics? Tôi đã benchmark"
date: 2026-10-19
lastmod: 2026-10-19
tags: [prometheus, postgresql, benchmark, metrics, time-series]
excerpt: "Trong một buổi phỏng vấn, Head of Platform hỏi tôi tại sao không dùng PostgreSQL cho metrics. Tôi trả lời bằng lý thuyết và không có con số nào chống lưng. Nên tôi đã chạy benchmark."
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "Tại sao không dùng PostgreSQL cho metrics? Tôi đã benchmark",
  "description": "Trong một buổi phỏng vấn, Head of Platform hỏi tôi tại sao không dùng PostgreSQL cho metrics. Tôi trả lời bằng lý thuyết và không có con số nào chống lưng. Nên tôi đã chạy benchmark.",
  "author": {
    "@type": "Person",
    "name": "Platform Engineering Team"
  },
  "publisher": {
    "@type": "Organization",
    "name": "Platform Engineering Blog"
  },
  "datePublished": "2026-10-19",
  "dateModified": "2026-10-19",
  "mainEntityOfPage": {
    "@type": "WebPage",
    "@id": "https://example.com/blog/prometheus-vs-postgres-metrics-vi"
  },
  "keywords": ["prometheus", "postgresql", "benchmark", "metrics", "time-series"],
  "articleSection": "Engineering"
}
</script>

<meta property="og:title" content="Tại sao không dùng PostgreSQL cho metrics? Tôi đã benchmark">
<meta property="og:description" content="Trong một buổi phỏng vấn, Head of Platform hỏi tôi tại sao không dùng PostgreSQL cho metrics. Tôi trả lời bằng lý thuyết và không có con số nào chống lưng. Nên tôi đã chạy benchmark.">
<meta property="og:type" content="article">
<meta property="og:article:published_time" content="2026-10-19">
<meta property="twitter:card" content="summary">

# Tại sao không dùng PostgreSQL cho metrics? Tôi đã benchmark

Buổi phỏng vấn là cho vị trí Platform Engineering tại một công ty tài chính tiêu dùng. Tôi chuẩn bị cho phần quen thuộc: mô tả hệ thống từng xây, bảo vệ một trade-off. Head of Platform bỏ qua tất cả và hỏi thẳng:

> "Tại sao không dùng PostgreSQL cho metrics?"

Tôi trả lời bằng những gì mình hiểu lúc đó: metrics chủ yếu là append, một series được nhận diện bằng label, và dữ liệu theo thứ tự thời gian nén tốt hơn nhiều so với row. Thành thật mà nói, tôi chưa chuẩn bị cho câu này, và tôi không hiểu sâu cả hai hệ thống. Tôi cố giải thích, nhưng anh ấy vẫn chưa bị thuyết phục, và tôi không trách được. Tôi nói được mình tin gì, không nói được vì sao.

Sau buổi phỏng vấn, tôi vẫn muốn có một lý do đàng hoàng để chọn cái này thay cái kia. "Prometheus hỗ trợ sẵn mọi thứ" là câu trả lời dễ, và tôi cố ý bỏ nó ra. Bài này là những gì tôi tìm được sau vài tuần đào. Kết luận là cả hai đều có thể là lựa chọn hợp lý, tùy nhu cầu, và phần còn lại của bài chỉ ra mỗi bên hợp ở đâu.

Nên tôi dựng một benchmark. Cùng dữ liệu, ba store, năm hình dạng truy vấn, và mọi con số dưới đây đến từ một run CI xanh.

## Bản tóm tắt

- **Đĩa:** Prometheus 12.18 bytes/sample, PostgreSQL chuẩn hóa 370.88, PostgreSQL JSONB 838.25. Tức là nhỏ hơn 30× và 69×.
- **Độ trễ:** Prometheus nhanh nhất ở bốn trong năm hình dạng. Postgres chuẩn hóa thắng cái thứ năm, một series trong 15 phút (0.14 ms so với 0.52 ms).
- **Counter rate trên cả 1,600 series:** 3.0 ms trên Prometheus, 63.9 ms trên Postgres chuẩn hóa, 874.3 ms trên JSONB. Khoảng 21× và 290×.
- **Bão hòa:** cùng một ramp tải, Prometheus hoàn thành 169,841 truy vấn, Postgres chuẩn hóa 4,894, JSONB 443.
- **Ghi:** Postgres COPY nạp dữ liệu trong 0.9 s (chuẩn hóa) và 2.7 s (JSONB). Prometheus mất 0.7 s ghi thật, cộng 144 s dừng mà tôi tự thêm.
- **Chưa đo:** CPU và RAM, và một Prometheus đã compact. Nói thêm ở cuối bài.

## Setup

Dataset là 2 job × 400 instance × 2 họ metric, tức 1,600 series. Mỗi series có 180 sample, cách nhau 15 giây, nên 288,000 sample mỗi backend trong cửa sổ 45 phút. Generator có seed cố định, nên mọi lần chạy sinh ra giá trị y hệt. Mỗi store chạy trong container giới hạn 1.5 CPU và 4 GB RAM. Mọi số trong bài đến từ run `gh-232.1`.

Kiểm tra parity: Prometheus giữ 286,400 sample, Postgres giữ 288,000, chênh 0.56%, nằm trong ngưỡng 2% tôi đặt cho cổng kiểm tra. Tôi cũng so tổng, nhưng chỉ để chẩn đoán. Instant query của PromQL và `DISTINCT ON` của Postgres hiểu "giá trị cuối" khác nhau, nên hai tổng chênh khoảng 3× và con số đó không nói gì về chất lượng dữ liệu.

## Đĩa

| Hệ thống | Sample giữ lại | Trên đĩa | Bytes / sample |
|--------|--------------|---------|----------------|
| Prometheus TSDB | 286,400 | 3.3 MB | 12.18 |
| PostgreSQL chuẩn hóa | 288,000 | 101.9 MB | 370.88 |
| PostgreSQL JSONB | 288,000 | 230.2 MB | 838.25 |

<svg role="img" aria-labelledby="chart-disk-title chart-disk-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-disk-title">Dung lượng đĩa — bytes trên mỗi sample</title>
  <desc id="chart-disk-desc">Biểu đồ cột ngang so sánh bytes trên sample: Prometheus 12.18, PostgreSQL chuẩn hóa 370.88, PostgreSQL JSONB 838.25.</desc>
  <rect x="80" y="30" width="1" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="28" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="64" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres chuẩn hóa</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres JSONB</text>
  <text x="86" y="59" font-family="system-ui, sans-serif" font-size="12" fill="#374151">12.18</text>
  <text x="112" y="119" font-family="system-ui, sans-serif" font-size="12" fill="#374151">370.88</text>
  <text x="148" y="179" font-family="system-ui, sans-serif" font-size="12" fill="#374151">838.25</text>
</svg>

Phần lớn khoảng cách là văn bản nhãn. JSONB lặp toàn bộ label document trên từng row, và `env="production"` bị lưu cả triệu lần vì Postgres không intern string. Schema chuẩn hóa giữ row nhỏ, nhưng vẫn mang label text trên mỗi row và index `(series_id, time)` cộng thêm trọng lượng của nó. Prometheus lưu mỗi label set một lần trong index và ghi mỗi sample như một timestamp và một value.

Hai con số đó rẻ vì cách chúng được mã hóa. Timestamp dùng delta-of-delta, nên với interval scrape ổn định chúng tốn khoảng một bit mỗi cái. Value dùng Gorilla XOR. Không phần nào cần một lượt compaction. Nó xảy ra ngay khi sample đến, vì chúng đến theo thứ tự thời gian.

## Tại sao Prometheus nhỏ hơn và nhanh hơn

Prometheus đạt được điều đó bằng cách không làm bốn việc Postgres buộc phải làm:

| Prometheus không làm | Tiết kiệm được gì |
|--------------------|---------------|
| UPDATE / DELETE tại chỗ | Ghi append-only và theo thứ tự thời gian |
| Một row mỗi sample | Label set là identity, nên nhãn không bị lặp mỗi sample |
| Index B-tree đa mục đích | Inverted index trên series ref, không phải row |
| MVCC row versioning | Không tuple header 23 byte mỗi row, không WAL record mỗi row |

Hai điểm cuối giải thích phần lớn khoảng cách bytes/sample.

## Ghi

| Hệ thống | Thời gian ghi | Throughput khi ghi |
|--------|-----------|--------------------------|
| PostgreSQL JSONB | 2.7 s (COPY) | ~106k sample/s |
| PostgreSQL chuẩn hóa | 0.9 s (COPY) | ~311k sample/s |
| Prometheus | 0.7 s | ~421k sample/s |

Wall-clock của Prometheus dài hơn nhiều, khoảng 144 s. Đó là `REQUEST_DELAY` tôi tự đặt, một lần dừng sau mỗi batch remote-write để WAL không bị dội. Nó không nói gì về tốc độ của Prometheus, nên hãy so thời gian ghi ở bảng trên và bỏ qua wall-clock. Postgres không có khoảng dừng đó.

## Năm truy vấn

Các nhãn q1–q5 chỉ là tên gọi. Đây là chính xác mỗi cái chạy gì; mỗi hình dạng tồn tại để kiểm tra một claim khác nhau.

| Query | Đo cái gì | PromQL | SQL Postgres (schema chuẩn hóa) |
|-------|--------------------|--------|----------------------------------|
| q1 | Rate trên mọi series counter | `sum(rate(http_requests_total[5m]))` | `lag(value) OVER (PARTITION BY series_id ORDER BY time)`, rồi cộng các delta |
| q2 | Queue depth nhóm theo job | `sum by (job) (queue_depth)` | `GROUP BY job` với `avg(value)` |
| q3 | p90 mỗi instance của `job-00`, một giờ | `quantile_over_time(0.9, queue_depth{job="job-00"}[1h])` | `percentile_cont(0.9) WITHIN GROUP (ORDER BY value)`, nhóm theo instance |
| q4 | Queue depth trung bình của `job-00`, toàn cửa sổ | `sum(queue_depth{job="job-00"})` | `avg(value)` với `job = 'job-00'` |
| q5 | Một instance, 15 phút cuối | `queue_depth{instance="host-0000"}` | `avg(value)` với `instance = 'host-0000'` |

Schema JSONB chạy cùng câu SQL nhưng trên một document `labels` (`labels->>'job'`, `labels @> '{"__name__":...}'`) thay vì join `series_dim`.

Bảng này cũng cho thấy một điểm không đối xứng. Prometheus chạy mỗi PromQL như một instant query ở cuối cửa sổ; Postgres gộp trên cả cửa sổ. Với q1, Prometheus đọc 5 phút cuối còn Postgres đi hết 45 phút. Nên đây là so chi phí của hình dạng trên mỗi engine, không phải so hai bên quét cùng số row. Chỗ nào điều đó làm thay đổi cách đọc, tôi nói rõ.

## Độ trễ truy vấn

Một kết nối, cache warm, 30 lượt đo sau 3 lượt warm-up, truyền `time=` tường minh ở cuối cửa sổ dữ liệu. Mọi truy vấn đều trả về row. Harness ghi số row khớp mỗi truy vấn và từ chối báo latency cho kết quả rỗng, vì kết quả rỗng sẽ trông như truy vấn nhanh nhất có thể.

| Query | Hình dạng | Prometheus p50 / p95 | Postgres JSONB p50 / p95 | Postgres chuẩn hóa + BRIN p50 / p95 |
|-------|-------|----------------------|--------------------------|--------------------------------------|
| q1 | Rate trên mọi counter | 3.0 / 4.5 ms | 874.3 / 910.8 ms | 63.9 / 65.1 ms |
| q2 | Sum theo job | 2.6 / 4.0 ms | 71.0 / 77.8 ms | 31.1 / 32.6 ms |
| q3 | p90 mỗi instance, 1 h | 9.5 / 18.7 ms | 45.5 / 86.2 ms | 12.2 / 12.4 ms |
| q4 | Một job, toàn cửa sổ | 1.6 / 1.7 ms | 21.8 / 22.0 ms | 7.1 / 7.3 ms |
| q5 | Một instance, 15 phút | 0.52 / 0.58 ms | 0.23 / 0.38 ms | 0.14 / 0.32 ms |

<svg role="img" aria-labelledby="chart-q-title chart-q-desc" viewBox="0 0 640 270" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-q-title">Độ trễ truy vấn theo hình dạng — p50, milliseconds (thang log)</title>
  <desc id="chart-q-desc">Cột nhóm so sánh p50 cho năm hình dạng truy vấn giữa Prometheus, PostgreSQL JSONB và PostgreSQL chuẩn hóa. Prometheus thắng q1 đến q4; Postgres chuẩn hóa thắng q5.</desc>
  <text x="16" y="20" font-family="system-ui, sans-serif" font-size="12" fill="#6b7280">p50 ms (thang log)</text>
  <g font-family="system-ui, sans-serif" font-size="11" fill="#374151">
    <text x="16" y="70">q1</text><text x="16" y="115">q2</text><text x="16" y="160">q3</text><text x="16" y="205">q4</text><text x="16" y="250">q5</text>
  </g>
  <g>
    <rect x="50" y="52" width="12" height="16" fill="#10b981" rx="2"/>
    <rect x="66" y="70" width="110" height="16" fill="#ef4444" rx="2"/>
    <rect x="66" y="88" width="89" height="16" fill="#3b82f6" rx="2"/>
    <rect x="50" y="97" width="21" height="16" fill="#10b981" rx="2"/>
    <rect x="66" y="115" width="83" height="16" fill="#ef4444" rx="2"/>
    <rect x="66" y="133" width="70" height="16" fill="#3b82f6" rx="2"/>
    <rect x="50" y="142" width="38" height="16" fill="#10b981" rx="2"/>
    <rect x="66" y="160" width="77" height="16" fill="#ef4444" rx="2"/>
    <rect x="66" y="178" width="50" height="16" fill="#3b82f6" rx="2"/>
    <rect x="50" y="187" width="21" height="16" fill="#10b981" rx="2"/>
    <rect x="66" y="205" width="61" height="16" fill="#ef4444" rx="2"/>
    <rect x="66" y="223" width="40" height="16" fill="#3b82f6" rx="2"/>
  </g>
  <g font-family="system-ui, sans-serif" font-size="11" fill="#6b7280">
    <rect x="420" y="52" width="10" height="10" fill="#10b981" rx="1"/><text x="436" y="61">Prometheus</text>
    <rect x="420" y="70" width="10" height="10" fill="#ef4444" rx="1"/><text x="436" y="79">Postgres JSONB</text>
    <rect x="420" y="88" width="10" height="10" fill="#3b82f6" rx="1"/><text x="436" y="97">Postgres chuẩn hóa + BRIN</text>
  </g>
</svg>

Prometheus thắng q1 đến q4, Postgres chuẩn hóa thắng q5. Ba điểm trong bảng đáng nhìn kỹ.

q1 là hình dạng counter-reset. Trong Postgres bạn viết `lag() OVER (PARTITION BY ...)`, nó phải sắp xếp và đi qua từng row. JSONB còn trả heap fetch mỗi row, đó là nguồn gốc của 874 ms. Chuẩn hóa và join một bảng dimension nhỏ kéo xuống 64 ms. Prometheus đọc một slice đã được sắp xếp từ lúc ghi, và mất 3 ms. Cửa sổ ở đây khác nhau như đã nói: Prometheus trả lời rate 5 phút, Postgres đi cả cửa sổ 45 phút, nên một phần khoảng cách là khối lượng chứ không phải thuật toán. Dù vậy, window function cộng heap fetch vẫn là phần lớn thiệt hại.

q3 làm tôi bất ngờ. Tôi tưởng percentile là sân nhà của Postgres: `percentile_cont` sắp xếp trong database và không có giới hạn sample, còn `quantile_over_time` sắp xếp trong RAM dưới `--query.max-samples`. Với 400 series và 45 phút, Prometheus vẫn thắng (9.5 ms so với 12.2 ms). Tôi nghĩ điều đó sẽ đổi khi N lớn hơn nhiều, nên tôi không kết luận gì từ đây.

q5 là chiến thắng rõ ràng của Postgres. Postgres lấy trung bình 60 row của một series, một tra cứu B-tree, và 0.14 ms là rất khó đánh bại. Prometheus trả lời selector tại một thời điểm, nên nó đọc một sample thay vì một khoảng. Nếu ai nói Prometheus thắng mọi hình dạng, hãy xem bảng của họ.

## Load test

Tôi dùng k6 để ramp từ 10 lên 2,000 request/giây (mô hình mở, `ramping-arrival-rate`) với ngưỡng p95 500 ms. Cả ba đều vượt ngưỡng (exit code 99), nên đây là đầu dò bão hòa và tôi không đọc nó như phép đo latency. Nó chỉ chạy một hình dạng, q1:

```
sum(rate(http_requests_total[5m]))
```

Bên Postgres, tương đương là câu `lag() OVER (PARTITION BY ...)`.

| Hệ thống | Hoàn thành | Bị từ chối | p50 | p95 |
|--------|-----------|---------|-----|-----|
| Prometheus | 169,841 | 299,958 | 1.6 s | 1.8 s |
| PostgreSQL chuẩn hóa | 4,894 | 464,906 | 3.6 s | 5.1 s |
| PostgreSQL JSONB | 443 | 469,356 | 41.4 s | 52.5 s |

<svg role="img" aria-labelledby="chart-break-title chart-break-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-break-title">Số iteration hoàn thành trên toàn bộ ramp</title>
  <desc id="chart-break-desc">Biểu đồ cột ngang số iteration hoàn thành: Prometheus 169,841, PostgreSQL chuẩn hóa 4,894, PostgreSQL JSONB 443.</desc>
  <rect x="80" y="30" width="64" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="2" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="1" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres chuẩn hóa</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="#374151">Postgres JSONB</text>
  <text x="148" y="59" font-family="system-ui, sans-serif" font-size="12" fill="#374151">169,841</text>
  <text x="86" y="119" font-family="system-ui, sans-serif" font-size="12" fill="#374151">4,894</text>
  <text x="86" y="179" font-family="system-ui, sans-serif" font-size="12" fill="#374151">443</text>
</svg>

Mỗi row cộng lại khoảng 469,800, đúng bằng toàn bộ ramp. Nên "hoàn thành" đếm cả run, không phải thời điểm p95 vượt 500 ms. Trên cùng một ramp, Prometheus phục vụ khoảng 35× số truy vấn so với Postgres chuẩn hóa và 383× so với JSONB.

Con số 1.6 s của Prometheus là p50 khi có hàng trăm client cùng lúc. Cùng truy vấn đó mất 3 ms trên một kết nối, phần còn lại là queueing. Bảng latency cho biết một truy vấn tốn bao nhiêu; bảng này cho biết hệ thống chịu được bao nhiêu tải trước khi tail sụp.

## Tôi đã đo gì, và đo thế nào

Năm con số, mỗi con số có một quy tắc rõ ràng:

| Chỉ số | Cách đo | Trả lời câu gì |
|--------|---------------------|-----------------|
| Đĩa | Bytes trên đĩa sau khi nạp 288,000 sample, chia cho số sample giữ lại. Gồm WAL của Prometheus và index của Postgres. | Giữ một sample tốn bao nhiêu? |
| Độ trễ truy vấn | p50/p95 qua 30 lượt sau 3 lượt warm-up, một kết nối, cache warm, `time=` tường minh ở cuối cửa sổ. Kết quả rỗng bị loại. | Hình dạng truy vấn này tốn bao nhiêu? |
| Ghi | Wall time của COPY (Postgres) và remote-write (Prometheus), với 144 s pacing của Prometheus bị loại khỏi throughput. | Đường ghi nhận dữ liệu nhanh đến đâu? |
| Bão hòa | k6 ramp mô hình mở từ 10 lên 2,000 req/s, ngưỡng p95 500 ms, đếm iteration hoàn thành trên toàn ramp. | Chịu được bao nhiêu tải trước khi tail sụp? |
| Parity | Số sample mỗi backend, trong ngưỡng 2%. | Ba store có giữ cùng dữ liệu không? |

Điều giữ phép so sánh công bằng: một dataset có seed, cùng giới hạn 1.5 CPU / 4 GB cho mọi container, và một biểu thức cố định cho mỗi backend ở cả năm hình dạng. Chỗ nào hai engine không làm cùng một khối lượng việc, phần caveat ở cuối nói rõ.

## Nơi Postgres là lựa chọn tốt hơn

- Tra cứu một series, khoảng ngắn. q5 cho thấy: 0.14 ms so với 0.52 ms.
- Percentile không giới hạn sample. `percentile_cont` không bị ràng buộc bởi `--query.max-samples` (mặc định 50M), và khi N rất lớn điều đó quan trọng.
- Join. PromQL không join được metrics với khách hàng hay giao dịch, còn SQL thì được.
- Retention. Drop một partition là O(1). Prometheus ghi tombstone và viết lại block ở lần compaction kế tiếp.

## Những gì bài này chưa cho thấy

1. Tổng của hai engine chênh khoảng 3× (149.1M so với 452.1M) vì chúng định nghĩa "giá trị cuối" khác nhau. Chỉ số sample count là check thật.
2. Prometheus mất 0.56% sample. Tôi chưa truy ra vì sao.
3. Không có gì được compact. 45 phút nằm dưới head span khoảng 3 h, nên con số đĩa là cho head chunk và block steady-state có thể khác.
4. Không có số CPU hay RAM. Runner CI không đọc được container metrics. Chúng về Grafana Cloud nhưng không có trong bài này.
5. Query sweep là một kết nối và warm. Nó chỉ đo chi phí truy vấn. Run k6 là góc nhìn concurrency.
6. Scale nhỏ, 1,600 series và 288,000 sample. Nó cho thấy hình dạng chi phí và nói rất ít về cách chúng tăng. q3 đặc biệt có thể đảo.
7. Tôi dùng `prom/prometheus:latest` và `postgres:latest` và không ghi lại digest của image.
8. Con số ~1.37 B/sample của Gorilla và claim nén 90-95% của TimescaleDB là từ paper và tài liệu, không phải từ lần chạy của tôi.
9. Hai engine không phải lúc nào cũng quét cùng một cửa sổ. Prometheus trả lời instant query ở cuối cửa sổ; Postgres gộp trên cả cửa sổ. q1 là ví dụ rõ nhất, rate 5 phút so với đi bộ 45 phút. Bảng so chi phí của hình dạng trên mỗi engine, không phải cùng một khối lượng việc.

## Tôi sẽ nói gì bây giờ

Nếu anh ấy hỏi lại, tôi sẽ không nói "Postgres không làm được", vì nó làm được. Tôi sẽ nói nó làm được, đây là cái giá, và đây là nơi nó thắng:

> PostgreSQL lưu được metrics, nhưng mỗi sample phải trả cho tuple header, index entry, WAL, và một heap fetch mỗi khi bạn lọc theo label. Trong test của tôi, cái giá đó là 30× đến 69× dung lượng đĩa và nhanh nhất là chậm hơn khoảng 290× ở truy vấn counter-rate, dù một phần khoảng cách cuối là do Postgres phải quét cửa sổ rộng hơn. Prometheus bỏ hết những thứ đó: không UPDATE, không row mỗi sample, và decompression gần như miễn phí. Đổi lại nó cứng nhắc. Nếu bạn cần join, percentile không giới hạn sample, hoặc nhiều năm retention cạnh dữ liệu nghiệp vụ, Postgres là lựa chọn hợp lý và đôi khi là lựa chọn tốt hơn.

Nên câu trả lời phụ thuộc workload, và bảng dưới đây là cách tôi sẽ quyết định.

| Tình huống | Dùng |
|-----------|-----|
| Giám sát hạ tầng, Kubernetes, cảnh báo tức thời | Prometheus |
| Nhiều năm retention, metrics join với dữ liệu nghiệp vụ | PostgreSQL + TimescaleDB, hoặc hybrid |
| Percentile SQL không giới hạn, ad-hoc reporting | PostgreSQL |
| High cardinality và chi phí đĩa quan trọng | Prometheus |

## Dự đoán ban đầu có đúng không?

Trong buổi phỏng vấn tôi đưa ra hai nhận định không có số liệu. Đây là kết quả đối chiếu.

| Tôi đã nói | Đo được gì | Kết luận |
|-------------|-----------------|---------|
| Postgres sẽ tốn nhiều đĩa hơn, chủ yếu vì index. | Nhiều hơn 30× mỗi sample so với Prometheus ở schema chuẩn hóa, 69× với JSONB. Nhưng hai schema Postgres giữ cùng số row mà vẫn chênh 2.3× (838 so với 371 B/sample), và khoảng chênh đó là label document bị lặp trên từng row. | Đúng ở chỗ đĩa là vấn đề; sai ở chỗ index là nguyên nhân chính. |
| `rate()` sẽ chậm hơn trên Postgres. | q1: 3.0 ms trên Prometheus, 63.9 ms chuẩn hóa, 874.3 ms JSONB. | Đúng hướng. Độ lớn phụ thuộc schema và cửa sổ: một phần khoảng cách là Postgres quét 45 phút còn Prometheus quét 5 phút. |
| Tôi không nghĩ Postgres sẽ thắng hình dạng nào. | q5: 0.14 ms chuẩn hóa so với 0.52 ms Prometheus. | Sai. Tra cứu ngắn, một series là điểm mạnh của Postgres. |

Cả hai nhận định đều đúng hướng, nên câu hỏi tiếp theo "tại sao?" mới là phép thử thật. Nhưng cơ chế tôi đưa ra quá đơn giản. Tôi đổ cho B-tree. Số đo chỉ vào identity: JSONB lưu cả label document trên từng row, và khoảng chênh 2.3× giữa hai schema Postgres — cùng row, khác cách xếp — chính là sự lặp đó. Schema chuẩn hóa tránh được nó nhưng vẫn giữ một index entry mỗi sample. Prometheus ghi label set một lần, sau đó chỉ còn timestamp và value.

Vậy câu trả lời phỏng vấn đúng một nửa. Chi phí là thật, nhưng phần lớn là chi phí lặp lại identity chứ không phải index, và khoảng cách tốc độ phụ thuộc hình dạng truy vấn chứ không phổ quát. Đó cũng là lý do kết luận là một bảng quyết định, không phải một người thắng.

## Reproduce

Mọi thứ chạy trên GitHub Actions tại `github.com/hungpham10/my-benchmarks`, trong `benchctl/`. Số đến từ run `gh-232.1` trên `main`, mất khoảng 33 phút. `scale=smoke` mất khoảng 3 phút và chỉ cho thấy pipeline hoạt động. `scale=pg` là so sánh đầy đủ và bao gồm sweep q1 đến q5.

*Số đo từ một run `scale=pg`, dùng `prom/prometheus:latest` và `postgres:latest`, mỗi hệ giới hạn 1.5 CPU / 4 GB. Tên công ty và Head of Platform không được nêu, theo yêu cầu.*
