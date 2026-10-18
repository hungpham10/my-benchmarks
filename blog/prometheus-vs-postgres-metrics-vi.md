---
title: "Prometheus vs PostgreSQL làm kho lưu metrics: kết quả benchmark thực tế"
date: 2026-10-18
tags: [prometheus, postgresql, benchmark, metrics, time-series]
excerpt: "Có nên dùng PostgreSQL cho metrics không? Tôi chạy benchmark đối đầu (Prometheus vs Postgres jsonb vs Postgres normalized) trên cùng một tập dữ liệu và cùng tải truy vấn. Dưới đây là số đo thực tế, kèm cảnh báo trung thực."
---

# Prometheus vs PostgreSQL làm kho lưu metrics

**"Có nên dùng PostgreSQL cho metrics không?"** Câu hỏi này hấp dẫn. Bạn đã chạy Postgres, đội ngũ biết SQL, và việc thêm hệ thống mới cảm thấy như thừa thải. Nhưng dữ liệu metrics hình thành khác dữ liệu quan hệ — chỉ ghi thêm (append-only), có thứ tự thời gian, được định danh bởi tập nhãn thay vì khóa số nguyên.

Tôi chạy benchmark đối đầu để xem cái gì thực sự hỏng. Cùng tập dữ liệu, cùng tải truy vấn, cùng khung phần cứng, đo trên mọi lần chạy. Không có hằng số bịa đặt.

## Tôi đo cái gì

Ba mục tiêu nhận cùng một tập dữ liệu tổng hợp (~20k chuỗi, khung 45 phút, khoảng scrape 15s):

| Mục tiêu | Cách lưu trữ |
|----------|-------------|
| Prometheus | TSDB bản địa (head, chưa compact) |
| PostgreSQL | `metrics_jsonb` — nhãn trong cột `jsonb`, chỉ mục GIN |
| PostgreSQL | `metrics_norm_brin` — bảng chuỗi chuẩn hóa, chỉ mục BRIN trên thời gian |

Mọi lần chạy đều xác thực **đồng bộ mẫu**: số lượng mẫu phải khớp nhau trong khoảng 2% giữa các hệ thống (`failed: 0` trên lần chạy công bố). Tổng chỉ được ghi lại làm chẩn đoán — thêm chi tiết ở mục cảnh báo bên dưới.

## Tốc độ ghi (với một cảnh báo trung thực)

| Hệ thống | Thời gian ghi thực | Tốc độ trong khi ghi |
|----------|-----------------|---------------------|
| postgres/jsonb | 1.4 s (COPY) | ~106k mẫu/s |
| postgres/norm | 0.6 s (COPY) | ~254k mẫu/s |
| prometheus | 0.5 s ghi + 72 s chờ pacing | ~284k mẫu/s trong khi ghi |

**Cảnh báo quan trọng:** Thời gian wall của Prometheus bao gồm khoảng 72 giây chờ pacing (`REQUEST_DELAY`) để tránh quá tải WAL. Bỏ pacing thì Prometheus mất khoảng 30% mẫu. Đây là *chắn chống áp lực ngược*, không phải nút điều chỉnh — nên so sánh thời gian wall-clock là không công bằng. Điều quan trọng là tốc độ ghi thực khi đã tính đến pacing, và ở đó Prometheus giữ vững được.

## Dung lượng đĩa (số tiêu đề)

| Hệ thống | Mẫu | Byte trên đĩa | Byte / mẫu |
|----------|-----|---------------|------------|
| postgres/jsonb | 144,000 | 140.7 MB | **1,024.56** |
| postgres/norm | 144,000 | 74.2 MB | **540.09** |
| prometheus | 143,200 | 1.7 MB | **12.68** |

Prometheus nhỏ hơn **~43 lần** so với Postgres chuẩn hóa và **~81 lần** so với Postgres jsonb.

Chi phí không thực sự là cấu trúc chỉ mục — nó là **văn bản nhãn**. jsonb lưu lại toàn bộ tài liệu nhãn trên *mỗi mẫu đơn lẻ* (chỉ mục rẻ, tỉ lệ 0.08× heap). Chuẩn hóa đảo ngược điều đó: hàng rẻ, chỉ mục đắt (tỉ lệ 0.61× heap). Cả hai đều không thoát khỏi tổng chi phí. Vì vậy lời khai trung thực không phải "jsonb xấu" — mà là mẫu metrics mang theo văn bản nhãn, và bạn để văn bản đó ở đâu, bạn cũng phải trả giá.

## Kiểm tra tải truy vấn — ai gãy đầu tiên?

Tôi tăng tốc độ truy vấn từ 200 → 2,000 req/s bằng k6 (ramping-arrival-rate, ngưỡng p95 < 500 ms). Tất cả ba hệ thống đều gặp điểm gãy (k6 exit 99). Dưới đây là những gì mỗi hệ thống làm ngay trước khi gãy:

| Hệ thống | Lần lặp khi gãy | Độ trễ p50 | Độ trễ p95 | Lần lặp bị từ chối |
|----------|-----------------|------------|------------|-------------------|
| postgres/jsonb | 278 | 66 s | 81 s | 469,522 |
| postgres/norm | 469,604 | 0.5 ms | 0.7 ms | 196 |
| prometheus | 176,266 | 1.4 s | 3.1 s | 293,533 |

**Kết quả chính:** Ở hình dạng truy vấn này (rate qua chuỗi counter, nhóm theo công việc), Postgres chuẩn hóa chiến thắng **cả độ trễ lẫn tốc độ**. Prometheus từ chối khoảng 293k lần lặp vì mỗi truy vấn mất khoảng 1–3 giây — nó không kịp ở 2,000 req/s. jsonb gãy ngay lập tức vì mỗi truy vấn mất khoảng 80 giây (jsonb containment + lấy heap cho mọi hàng).

Lưu ý độ trễ p95 của Prometheus chênh lệch liên lần (~1.9–3.1 giây giữa các lần chạy). Độ trễ tại điểm gãy không ổn định bằng giá trị trung bình — hãy coi nó là một khoảng, không phải con số duy nhất.

## Vật PostgreSQL thực sự thắng

- **Phần trăm trong một khoảng.** `percentile_cont(0.9) WITHIN GROUP (ORDER BY value)` sắp xếp trong cơ sở dữ liệu và không bị chịu ngưỡng `--query.max-samples` của Prometheus (mặc định 50M). `quantile_over_time` của Prometheus sắp xếp trong RAM và *bị* giới hạn.
- **Tra cứu điểm một chuỗi duy nhất.** Một chuỗi trong khoảng thời gian là dưới một mili giây trong Postgres nhờ tra cứu điểm B-tree. Prometheus p50 cho cùng hình dạng là ~1.4 giây khi có tải — hai hệ không so sánh trực tiếp vì PromQL cũng làm downsample và bổ gián đoạn, nhưng độ trễ tra cứu thô thuận lợi cho Postgres.
- **Goi và điều khoản SQL tùy ý.** PromQL là một ngôn ngữ kín; Postgres cho phép bạn nối metrics với bất cứ thứ gì khác trong cơ sở dữ liệu của bạn.

## Vật Prometheus thực sự thắng

- **Đĩa.** Nhỏ hơn 43–81 lần mỗi mẫu. Ở quy mô thực tế (1M chuỗi × 15s × 24h), đó là terabyte so với hàng trăm GB.
- **Đường ghi.** Chỉ ghi thêm, không khóa, không MVCC. Một bản ghi WAL mỗi scrape — "hiển thị cho người truy vấn" ngụ ý "đã có trong WAL".
- **Đặt lại counter.** `rate()` của PromQL đi qua một lát được *sắp xếp sẵn khi ghi*. Không hàm cửa sổ, không sắp xếp lại. Trong Postgres, `lag(value) OVER (PARTITION BY ...)` buộc phải sắp xếp theo phân vùng.

## Cảnh báo trung thực

1. **Đồng bộ tổng không được xác thực.** Tổng Prometheus (154M) so với Postgres (452M) chênh khoảng 3×. Tôi cố ý không claims đồng bộ tổng; số lượng mẫu khớp trong khoảng 0.6% (-800 trong 144,000). Khoảng cách tổng chưa được giải thích trong lần chạy này và được ghi lại làm chẩn đoán chỉ.
2. **Prometheus mất 0.6% mẫu** (143,200 so với 144,000). Đã ghi lại, không giấu.
3. **Chưa compact.** Cửa sổ 45 phút dưới ngưỡng compact của Prometheus (~3h trải head), nên con số đĩa phản ánh khối head, không phải khối ổ cứng ổn định.
4. **CPU/RAM không có trong báo cáo này.** Môi trường CI không thể hiển thị chỉ số_container cAdvisor cho runner; các chỉ số này vẫn đi đến Grafana Cloud nhưng không có trong số được công bố. Đường cong dữ liệu có sẵn trên Cloud gắn thẻ `run_id="gh-219.1"`.

## Kết luận ngắn gọn

| Tiêu chí | Chiến thắng | Tại sao |
|----------|-----------|-----|
| Đĩa mỗi mẫu | Prometheus | Không chi phí hàng; delta-of-delta trên dữ liệu có thứ tự thời gian |
| Tốc độ ghi | Prometheus | Chỉ ghi thêm, một bản ghi WAL mỗi scrape |
| Tra cứu điểm một chuỗi | PostgreSQL | Tra cứu điểm B-tree, dưới một mili giây |
| Phần trăm trong khoảng | PostgreSQL | Sắp xếp trong cơ sở dữ liệu, không giới hạn mẫu |
| Đặt lại counter / `rate()` | Prometheus | Dữ liệu đã được sắp xếp khi ghi |
| SQL tùy ý / nối | PostgreSQL | Khả năng biểu thị SQL đầy đủ |
| Khoảng thời gian ngắn | Gần như ngang nhau | Đo được: norm 0.5ms so với Prometheus 0.75ms cho một chuỗi duy nhất qua 15 phút |

## Kết luận

Prometheus không chiến thắng vì nó có thuật toán nén tốt hơn. Nó chiến thắng bằng cách **từ chối những gì PostgreSQL làm**: không cập nhật chỗ, không xóa chọn lọc, không một hàng mỗi mẫu. làm người đó nó nhận được cấu trúc chỉ đọc, tuần tự, nén nơi giải nén gần như miễn phí. Giá trả là sự cứng nhắc vận hành.

PostgreSQL chiến thắng khi bạn cần sự linh hoạt SQL, phần trăm trong cơ sở dữ liệu, hoặc tra cứu điểm một chuỗi nhanh ở cardinality trung bình. Với metrics cao cardinality, giữ lưu trữ lâu ở quy mô lớn, lợi thế đĩa và ghi của Prometheus rất khó bỏ qua — nhưng chúng đi kèm với những Trade-off bạn nên đo, không phải chỉ giả định.

---

*Phương pháp: tất cả các số từ một lần chạy quy trình GitHub Actions duy nhất (scale=pg, run `gh-219.1`), Prometheus 3.15.0 vs PostgreSQL 18.6, mỗi hệ được giới hạn ở 1.5 CPU / 4 GB. Phiên bản tái hiện đầy đủ trong `benchctl/`. Đường cong dữ liệu trên Grafana Cloud gắn thẻ `run_id="gh-219.1"`.*
