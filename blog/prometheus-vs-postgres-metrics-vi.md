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

Buổi phỏng vấn là cho vị trí Platform Engineering. Head of Platform bỏ qua các câu hỏi quen thuộc và hỏi thẳng:

> "Tại sao không dùng PostgreSQL cho metrics?"

Tôi nói những gì mình tin: metrics chủ yếu là append, một series được nhận diện bằng label, và dữ liệu theo thứ tự thời gian nén tốt hơn row. Anh ấy không bị thuyết phục, và tôi không trách được. Tôi nói được mình tin gì, không nói được vì sao. Nên tôi chạy benchmark.

## Bản tóm tắt

- **Đĩa:** Prometheus 12.18 bytes/sample, PostgreSQL chuẩn hóa 370.88, PostgreSQL JSONB 838.25. Tức là nhỏ hơn 30× và 69×.
- **Một series, 30 ngày, scrape 5 giây:** 6.3 MB trên Prometheus, 192.3 MB trên Postgres chuẩn hóa, 434.5 MB trên JSONB. Ở 10,000 series, đó là $4.70, $304 và $688 mỗi tháng cho lưu trữ GCP.
- **Độ trễ truy vấn:** Prometheus nhanh nhất ở bốn trong năm hình dạng. Postgres thắng cái thứ năm: một series trong 15 phút.
- **Counter rate trên toàn bộ series:** 3.0 ms trên Prometheus, 63.9 ms trên Postgres chuẩn hóa, 874.3 ms trên JSONB.
- **Dưới tải:** cùng một ramp, Prometheus hoàn thành 169,841 truy vấn, Postgres chuẩn hóa 4,894, JSONB 443.

Cả hai đều có thể là lựa chọn đúng. Tùy workload.

## Setup

- **Dữ liệu:** 1,600 series (2 job × 400 instance × 2 họ metric), 180 sample mỗi series, cách nhau 15 giây, 288,000 sample mỗi backend. Generator có seed cố định, nên mọi lần chạy giống hệt nhau.
- **Giới hạn:** mỗi store chạy trong container giới hạn 1.5 CPU và 4 GB RAM.
- **Schema Postgres:** "dùng Postgres cho metrics" có thể nghĩa là nhiều kiểu bảng rất khác nhau, nên tôi thử hai kiểu.
  - **JSONB:** một row mỗi sample, cả label set trong một cột `jsonb` với GIN index. Đây là kiểu đa số team viết trước.
  - **Chuẩn hóa:** label nằm trong bảng nhỏ `series_dim`, sample lưu dạng `(series_id, time, value)`, BRIN trên time và btree trên `(series_id, time)`. Đây gần như là mức tốt nhất Postgres làm được ở đây.

## Schema Postgres

Đây là hai kiểu đó. Nếu cả hai đều thua, kết quả nói về Postgres; nếu chỉ một kiểu thua, kết quả nói về schema.

```sql
-- Schema 1: label nằm trong một document. Kiểu đa số team viết trước.
CREATE TABLE metrics_jsonb (
    time    timestamptz      NOT NULL,
    labels  jsonb            NOT NULL,
    value   double precision NOT NULL
) PARTITION BY RANGE (time);

CREATE INDEX metrics_jsonb_labels_idx ON metrics_jsonb USING gin (labels jsonb_path_ops);
CREATE INDEX metrics_jsonb_time_idx   ON metrics_jsonb (time);

-- Schema 2: identity ở bảng riêng, BRIN trên time.
CREATE TABLE series_dim (
    id       int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    metric   text NOT NULL,
    job      text NOT NULL,
    instance text NOT NULL,
    region   text NOT NULL,
    service  text NOT NULL,
    UNIQUE (metric, job, instance)
);

CREATE TABLE metrics_norm_brin (
    time      timestamptz      NOT NULL,
    series_id int              NOT NULL,
    value     double precision NOT NULL
) PARTITION BY RANGE (time);

CREATE INDEX metrics_norm_brin_time_brin_idx
    ON metrics_norm_brin USING brin (time) WITH (pages_per_range = 32);
CREATE INDEX metrics_norm_brin_series_time_idx
    ON metrics_norm_brin (series_id, time);
```

Cả hai bảng cha đều partition theo ngày trên `time`. Loader tạo partition lúc chạy, vì dataset được đánh dấu thời gian ở tương lai và `initdb` chỉ thấy một `PGDATA` rỗng.

Schema đầu là kiểu đa số team chọn: một row mỗi sample, toàn bộ identity của series trong một document JSON, GIN cho containment. Nó cũng là kiểu lưu `env="production"` lặp lại trên từng row.

Schema thứ hai là mức tốt nhất Postgres làm được cho access pattern này: `series_id` 4 byte thay cho label text, BRIN trên time vì sample chỉ append và gần như theo thứ tự thời gian, và btree trên `(series_id, time)` cho các tra cứu một series. BRIN là câu trả lời bản địa của Postgres cho dữ liệu append-only: nó lưu min/max theo từng page range và tốn kilobytes thay vì megabytes. Nó có giúp được ở đây hay không là một trong những thứ run này đo.

## Đĩa

| Hệ thống | Trên đĩa | Bytes / sample |
|--------|---------|----------------|
| Prometheus | 3.3 MB | 12.18 |
| PostgreSQL chuẩn hóa | 101.9 MB | 370.88 |
| PostgreSQL JSONB | 230.2 MB | 838.25 |

<svg role="img" aria-labelledby="chart-disk-title chart-disk-desc" viewBox="0 0 640 230" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-disk-title">Dung lượng đĩa — bytes trên mỗi sample</title>
  <desc id="chart-disk-desc">Biểu đồ cột ngang so sánh bytes trên sample: Prometheus 12.18, PostgreSQL chuẩn hóa 370.88, PostgreSQL JSONB 838.25.</desc>
  <rect x="80" y="30" width="1" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="28" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="64" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Postgres chuẩn hóa</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Postgres JSONB</text>
  <text x="86" y="59" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">12.18</text>
  <text x="112" y="119" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">370.88</text>
  <text x="148" y="179" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">838.25</text>
</svg>

Phần lớn khoảng cách là identity bị lặp. JSONB lưu cả label document trên từng row, nên `env="production"` được ghi cả triệu lần. Schema chuẩn hóa tránh được điều đó, nhưng vẫn trả cho row header, index entry và WAL mỗi sample. Prometheus lưu mỗi label set một lần, sau đó chỉ còn timestamp và value. Timestamp dùng delta-of-delta, value dùng Gorilla XOR, nên một scrape ổn định gần như không tốn gì.

Điều đó nghĩa là gì trên thực tế? Một series scrape 5 giây trong 30 ngày:

| Lưu trữ | Một series | 10,000 series |
|-------|-----------|---------------|
| Prometheus | 6.3 MB | 63 GB |
| PostgreSQL chuẩn hóa | 192.3 MB | 1.92 TB |
| PostgreSQL JSONB | 434.5 MB | 4.35 TB |

Đây là ngoại suy từ bytes/sample đo được, nên hãy đọc như bậc độ lớn.

Trên GCP (`us-central1`, giá list tháng 10/2026, chỉ lưu trữ), Hyperdisk Balanced là $0.08 mỗi GiB-tháng và Cloud SQL for PostgreSQL SSD là $0.17 mỗi GiB-tháng:

| Lưu trữ | 1 series | 1,000 series | 10,000 series |
|-------|----------|--------------|---------------|
| Prometheus, Hyperdisk Balanced | $0.0005 | $0.47 | $4.70 |
| PostgreSQL chuẩn hóa, Cloud SQL SSD | $0.03 | $30 | $304 |
| PostgreSQL JSONB, Cloud SQL SSD | $0.07 | $69 | $688 |

Ở 10,000 series, đó là $304 một tháng trước khi tính instance, và $688 nếu label vẫn nằm trong JSONB. Storage HA nhân đôi giá Cloud SQL, backup tính riêng, và GCP tính theo GiB còn kích thước ở trên là decimal, nên nhân trực tiếp sẽ ra cao hơn khoảng 7%.

## Độ trễ truy vấn

Năm hình dạng truy vấn, chạy trên cả ba store (một kết nối, cache warm, p50 của 30 lượt):

| Hình dạng | Prometheus | Postgres chuẩn hóa | Postgres JSONB |
|-------|-----------|---------------------|----------------|
| q1: rate trên mọi counter | 3.0 ms | 63.9 ms | 874.3 ms |
| q2: sum theo job | 2.6 ms | 31.1 ms | 71.0 ms |
| q3: p90 mỗi instance, 1 h | 9.5 ms | 12.2 ms | 45.5 ms |
| q4: một job, toàn cửa sổ | 1.6 ms | 7.1 ms | 21.8 ms |
| q5: một instance, 15 phút | 0.52 ms | **0.14 ms** | 0.23 ms |

<svg role="img" aria-labelledby="chart-q-title chart-q-desc" viewBox="0 0 640 270" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-q-title">Độ trễ truy vấn theo hình dạng — p50, milliseconds (thang log)</title>
  <desc id="chart-q-desc">Cột nhóm so sánh p50 cho năm hình dạng truy vấn giữa Prometheus, PostgreSQL JSONB và PostgreSQL chuẩn hóa. Prometheus thắng q1 đến q4; Postgres chuẩn hóa thắng q5.</desc>
  <text x="16" y="20" font-family="system-ui, sans-serif" font-size="12" fill="currentColor" fill-opacity="0.7">p50 ms (thang log)</text>
  <g font-family="system-ui, sans-serif" font-size="11" fill="currentColor">
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
  <g font-family="system-ui, sans-serif" font-size="11" fill="currentColor" fill-opacity="0.7">
    <rect x="420" y="52" width="10" height="10" fill="#10b981" rx="1"/><text x="436" y="61">Prometheus</text>
    <rect x="420" y="70" width="10" height="10" fill="#ef4444" rx="1"/><text x="436" y="79">Postgres JSONB</text>
    <rect x="420" y="88" width="10" height="10" fill="#3b82f6" rx="1"/><text x="436" y="97">Postgres chuẩn hóa + BRIN</text>
  </g>
</svg>

- **q1** là khoảng cách lớn nhất. Postgres cần `lag() OVER (PARTITION BY ...)`, phải sắp xếp và đi qua từng row, còn JSONB thêm một heap fetch mỗi row. Prometheus đọc một slice đã được sắp xếp từ lúc ghi. Một phần khoảng cách là kích thước cửa sổ: Prometheus tính rate 5 phút, Postgres đi cả 45 phút.
- **q3** làm tôi bất ngờ. Tôi tưởng percentile sẽ có lợi cho Postgres, nhưng Prometheus vẫn thắng ở scale này. Tôi không kết luận cho đến khi N lớn hơn nhiều.
- **q5** là chiến thắng rõ ràng của Postgres. Một tra cứu ngắn trên một series là một cú B-tree, và 0.14 ms rất khó đánh bại.

## Dưới tải

Tôi ramp q1 từ 10 lên 2,000 request/giây bằng k6. Cả ba hệ thống cuối cùng đều sụp, nên đây là phép đo bão hòa, không phải latency.

| Hệ thống | Hoàn thành | p95 |
|--------|-----------|-----|
| Prometheus | 169,841 | 1.8 s |
| PostgreSQL chuẩn hóa | 4,894 | 5.1 s |
| PostgreSQL JSONB | 443 | 52.5 s |

<svg role="img" aria-labelledby="chart-break-title chart-break-desc" viewBox="0 0 640 230" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-break-title">Số iteration hoàn thành trên toàn bộ ramp</title>
  <desc id="chart-break-desc">Biểu đồ cột ngang số iteration hoàn thành: Prometheus 169,841, PostgreSQL chuẩn hóa 4,894, PostgreSQL JSONB 443.</desc>
  <rect x="80" y="30" width="64" height="42" fill="#10b981" rx="2"/>
  <rect x="80" y="90" width="2" height="42" fill="#3b82f6" rx="2"/>
  <rect x="80" y="150" width="1" height="42" fill="#ef4444" rx="2"/>
  <text x="70" y="60" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Prometheus</text>
  <text x="70" y="120" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Postgres chuẩn hóa</text>
  <text x="70" y="180" text-anchor="end" font-family="system-ui, sans-serif" font-size="13" fill="currentColor">Postgres JSONB</text>
  <text x="148" y="59" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">169,841</text>
  <text x="86" y="119" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">4,894</text>
  <text x="86" y="179" font-family="system-ui, sans-serif" font-size="12" fill="currentColor">443</text>
</svg>

Prometheus phục vụ khoảng 35× số truy vấn so với Postgres chuẩn hóa và 383× so với JSONB.

## Nơi Postgres là lựa chọn tốt hơn

- Tra cứu ngắn, một series (q5).
- Percentile không giới hạn sample. `percentile_cont` không bị ràng buộc bởi `--query.max-samples` của Prometheus, điều này quan trọng khi N rất lớn.
- Join. PromQL không join được metrics với khách hàng hay giao dịch; SQL thì được.
- Retention. Drop một partition là O(1), còn Prometheus phải viết lại block ở lần compaction kế tiếp.

## Tôi sẽ nói gì bây giờ

> PostgreSQL lưu được metrics, nhưng mỗi sample phải trả cho tuple header, index entry, WAL, và label identity mà nó phải lặp lại. Trong test của tôi, cái giá đó là 30× đến 69× dung lượng đĩa và chậm hơn tới khoảng 290× ở truy vấn counter-rate, dù một phần khoảng cách cuối là do Postgres quét cửa sổ rộng hơn. Prometheus tránh được tất cả: không UPDATE, không row mỗi sample, nén gần như miễn phí. Đổi lại nó cứng nhắc. Nếu bạn cần join, percentile không giới hạn sample, hoặc retention dài cạnh dữ liệu nghiệp vụ, Postgres là lựa chọn hợp lý và đôi khi là lựa chọn tốt hơn.

| Tình huống | Dùng |
|-----------|-----|
| Giám sát hạ tầng, cảnh báo tức thời | Prometheus |
| High cardinality, chi phí đĩa quan trọng | Prometheus |
| Metrics join với dữ liệu nghiệp vụ, retention dài | PostgreSQL |
| Báo cáo SQL ad-hoc, percentile không giới hạn | PostgreSQL |

Câu trả lời phỏng vấn của tôi đúng một nửa. Đĩa và `rate()` tệ hơn trên Postgres, như tôi đã nói. Nhưng tôi đổ cho index, còn số liệu chỉ vào identity: hai schema Postgres giữ cùng số row mà vẫn chênh 2.3×, và khoảng chênh đó là label document bị lặp trên từng row. Và tôi không nghĩ Postgres sẽ thắng hình dạng nào, nhưng nó thắng một cái.

## Hạn chế

- Scale nhỏ (1,600 series, 288,000 sample). Nó cho thấy hình dạng chi phí, không cho thấy cách chúng tăng, và q3 có thể đảo.
- Run kéo dài 45 phút và không có compaction, nên số đĩa là cho head data.
- Hai engine không phải lúc nào cũng quét cùng cửa sổ (q1 là ví dụ rõ nhất), nên latency so chi phí theo hình dạng, không phải cùng một khối lượng việc.
- Prometheus giữ ít hơn Postgres 0.56% sample, và tôi chưa tìm ra vì sao.
- Tôi không đo CPU hay RAM, và dùng image `latest` mà không ghi lại digest.

Toàn bộ code nằm trong `benchctl/` tại `github.com/hungpham10/my-benchmarks`; các con số đến từ run `gh-232.1`.
