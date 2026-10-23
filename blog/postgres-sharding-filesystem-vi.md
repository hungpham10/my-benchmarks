---
title: "Sharding một instance: đo 4 model × 4 filesystem"
date: 2026-10-21
lastmod: 2026-10-22
lang: vi
tags: [postgresql, benchmark, sharding, filesystem, index, storage]
author: Platform Engineering Team
excerpt: "Khi làm benchmark cho vài công ty công nghệ, tôi thấy một mẫu lặp lại: data directory của PostgreSQL nằm trên XFS mà không ai ghi lại lý do. Nên tôi dựng harness và đo: sharding một bảng thành 32 bảng trên cùng một instance có giúp gì, và bốn filesystem phổ biến khác nhau ra sao với cùng workload đó."
description: "Sharding một bảng thành 32 bảng trên cùng một instance có giúp gì, và ext4/XFS/btrfs/ZFS khác nhau ra sao với cùng workload? Một benchmark trên 10M, 50M và 100M rows, với mọi loại index có sẵn của Postgres."
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "Sharding một instance: đo 4 model × 4 filesystem",
  "description": "Sharding một bảng thành 32 bảng trên cùng một instance có giúp gì, và ext4/XFS/btrfs/ZFS khác nhau ra sao với cùng workload? Một benchmark trên 10M, 50M và 100M rows.",
  "inLanguage": "vi",
  "author": { "@type": "Person", "name": "Platform Engineering Team" },
  "publisher": { "@type": "Organization", "name": "Platform Engineering Blog" },
  "datePublished": "2026-10-21",
  "dateModified": "2026-10-22",
  "mainEntityOfPage": {
    "@type": "WebPage",
    "@id": "https://example.com/blog/postgres-sharding-filesystem-vi"
  },
  "keywords": ["postgresql", "sharding", "benchmark", "filesystem", "xfs", "ext4", "btrfs", "zfs"],
  "articleSection": "Engineering"
}
</script>

<meta property="og:title" content="Sharding một instance: đo 4 model × 4 filesystem">
<meta property="og:description" content="Sharding một bảng thành 32 bảng trên cùng một instance có giúp gì, và bốn filesystem phổ biến khác nhau ra sao với cùng workload?">
<meta property="og:type" content="article">
<meta property="og:article:published_time" content="2026-10-21">
<meta name="twitter:card" content="summary">

# Sharding một instance: đo 4 model × 4 filesystem

Nhận benchmark cho vài công ty công nghệ, tôi thấy một mẫu lặp lại: **data directory của PostgreSQL nằm trên XFS**. Không ai viết lý do vào tài liệu — nó chỉ nằm trong cách dự án dựng máy. Hỏi thì câu trả lời thường là "ext4 mới là mặc định" hoặc "XFS nhanh hơn", nhưng không ai có số kèm theo.

Cùng lúc đó có một câu hỏi khác: nếu chưa tách được sang instance thứ hai, **sharding một bảng thành nhiều bảng ngay trên một instance** có giúp gì không? Tách sang nhiều instance thì ai cũng biết là dễ — thêm fsync lane, thêm CPU, thêm RAM. Nhưng phần lớn đội chưa được phép làm thế. Vậy phần "một instance" thì sao?

Hai câu hỏi, một harness. Tôi dựng bốn model sharding, chạy trên bốn filesystem, với 10M rows mặc định và quét thêm 50M/100M. Mọi con số dưới đây là số đo; chỗ nào chỉ là suy luận từ code hoặc phép tính, tôi nói rõ.

## Tóm tắt nhanh

- **Sharding không giúp gì cho ghi.** Một bảng so với 32 bảng chênh trong khoảng nhiễu ở mọi size, mọi filesystem, cả hai đợt đo. Commit đi qua một `WALWriteLock` và một `ProcArrayLock` của instance — hai lock này không biết bạn ghi vào bảng nào.
- **Sharding chỉ giúp read khi ứng dụng route đúng shard, và lợi ích nằm ở cold cache và tail latency, không ở median.** Warm p50 chỉ chênh 0.04–0.08 ms; p95 chênh 5–16×; cold chênh 39–120×.
- **Khoảng cách "bốn bậc độ lớn" trong lần đo đọc đầu tiên là artifact của harness**, không phải lợi ích của sharding: thiếu `ANALYZE` sau seed và dùng `random()` trong `WHERE` khiến mỗi nhánh của view thành một seq scan.
- **Không filesystem nào nhanh nhất một cách ổn định.** Cùng scenario, cùng filesystem, giữa hai đợt đo lệch tới 1.95× (btrfs 4 448 → 8 656 rows/s) — lớn hơn khoảng cách giữa các filesystem (1.4–1.9×).
- **XFS không hề chậm, nhưng cũng không có bằng chứng nó nhanh hơn ext4** trong workload này. Cả bốn FS nằm trong ~2× của nhau.
- **fsync microbenchmark không dự đoán được throughput ghi.** btrfs có fsync chậm nhất (561 µs/op) nhưng lại ghi nhanh nhất trong đợt 2; XFS có fsync 393 µs và ghi 5 218 rows/s. Group commit làm phần lớn khác biệt biến mất.
- **Số connection là một điểm gãy (knee), không phải núm vặn.** Ghi: p95 3 ms ở 16 connections → 82 ms ở 128 → 232 ms ở 500, throughput *giảm*. Read có knee muộn hơn, ở 16 connections cho 4 vCPU.
- **Chọn index đáng giá tới 1 800×** với đúng shape: BRIN trả lời range 7 ngày trong 1.7 ms, seq scan mất 3.1 s.
- **Toàn bộ setup gói trong một bảng**: một bảng `users`, một pool ~16 connections, mỗi query shape một index, ext4 hoặc XFS, và `ANALYZE` sau khi seed. Đó là câu trả lời cho "minimum viable production"; caveat nằm ở cuối bài.

## Setup

- **Phần cứng:** 4 vCPU, 8 GB, mỗi lần chạy một container riêng, PostgreSQL `latest`, WAL trên chính filesystem đang đo, `shared_buffers` 1 GB, `max_connections` 600.
- **Schema:** `users(user_id bigint PK, email citext UNIQUE, name, created_at)`, seed phía server, sau đó chạy k6 closed-loop insert (mỗi arrival một transaction nhỏ) trong 90 giây.
- **Bốn filesystem:** ext4, XFS, btrfs, ZFS. Mỗi FS trên một loop device riêng (trừ ext4 chạy native), `noatime`, không nén — nén sẽ là đo compressor chứ không phải filesystem.
- **Index lab:** một bảng `docs` 10M rows riêng; mỗi lần chạy gắn đúng một index theo access method: `brin_time`, `btree_name`, `hash_rating`, `gist_point`, `spgist_point`, `spgist_name`, `gin_trgm`, cộng lần chạy không index (`none`).
- **Đo read hai lần:** warm và cold, định nghĩa ngay dưới.

### Ba model sharding, ba tình huống khác nhau

Cả ba chạy trên **cùng một instance**, cùng schema, cùng 10M rows, cùng 90 giây; chỉ khác cách bố trí bảng và connection pool. M0 là mốc so sánh; M1 và M2 là hai cách "sharding" mà team thường làm khi chưa được tách instance.

| Model | Bố trí | Connections | Tình huống thực tế | Câu hỏi nó trả lời |
|---|---|---|---|---|
| **M0** | 1 bảng `users`, 1 pool | 16 | Ứng dụng "cứ dùng Postgres": một bảng duy nhất, ORM trỏ thẳng vào. Đây là baseline. | Không sharding thì sao? |
| **M1-P8** | 8 schema, mỗi schema 1 bảng, mỗi schema 1 pool; `user_id % 8` chọn pool | 128 | Tách theo tenant/service: mỗi nhóm một pool riêng, kỳ vọng nhiều pool = ghi song song. | Thêm pool và connection có tăng throughput? |
| **M1-P32** | 32 schema, mỗi schema 1 bảng, mỗi schema 1 pool | 500 | Cùng cơ chế, đẩy lên 32 pool để xem có scale tiếp không. | Thêm nữa thì sao? |
| **M2** | 32 bảng `users_00`…`users_31` trong 1 schema, 1 pool; ứng dụng hash `user_id` ra hậu tố | 16 | Sharding mức ứng dụng: chia bảng để mỗi bảng nhỏ hơn, vẫn một instance, một pool. | Chia bảng có giúp gì khi chưa tách instance? |

M1-P8 và M1-P32 là cùng một cơ chế, chỉ khác số schema — nên kết quả của chúng nói về **connection**, không nói về sharding.

### Warm cache và cold cache là gì

Postgres đọc dữ liệu theo page 8 KB, và một page có thể nằm trong hai lớp cache: `shared_buffers` của Postgres và page cache của OS (RAM). Nếu page đã có ở một trong hai, read trả về từ RAM và **không chạm đĩa**.

- **Warm cache** — chạy workload một lần cho dữ liệu nằm trong RAM, rồi đo lại. Phép đo này trả lời "query plan và routing có tốt không", nhưng **không** nói gì về filesystem, vì đĩa không tham gia.
- **Cold cache** — restart container (xoá `shared_buffers`) rồi drop page cache của host (`/proc/sys/vm/drop_caches`), buộc read phải xuống đĩa. Đây là phép đo duy nhất trả lời "filesystem này đọc nhanh hay chậm". Mỗi hình dạng query được reset cache riêng, vì một phép quét tuần tự làm ấm cả bảng và phép đo sau sẽ gọi đó là "cold" một cách nói dối.

Vì vậy các bảng read bên dưới đều có cả số warm và cold: số cold chứng minh đĩa nằm trên đường đi, số warm cho biết khi dữ liệu đã trong RAM thì routing còn đáng bao nhiêu.

Một ngoại lệ: trên ZFS, `drop_caches` không drop ARC (cache riêng của ZFS), nên "cold" không tái lập được — cùng một query đo 71 ms lần này và 743 ms lần khác. Số cold của ZFS vì thế chỉ nên đọc như một cận trên, không phải số so sánh.

### Container không format được filesystem — và cách vòng qua

Đây là chỗ dễ sai nhất của một benchmark filesystem.

Container **không** format được filesystem. Filesystem phải nằm trên máy chạy Docker daemon, rồi container bind-mount một thư mục bên trong nó:

```
/mnt/benchctl-fs/fs/xfs.img        file sparse trên đĩa của runner
  └─ losetup --direct-io=on        -> /dev/loop0
       └─ mkfs.xfs                 -> mount tại /mnt/benchctl-fs/mnt/xfs
            └─ bind mount          -> data directory của container
```

`--direct-io=on` không phải chi tiết trang trí. Không có nó, page cache của host nuốt mọi ghi trước khi tới đĩa, và filesystem đang đo **không bao giờ chạm đĩa** — lúc đó benchmark đo cache của host.

Cái giá: mỗi lần chạy là một runner mới, với ổ backing riêng. Đó chính là nguồn nhiễu lớn nhất của bài này, và là lý do tôi không xếp hạng filesystem như một bảng điểm.

### Đo bằng hai nguồn, vì mỗi nguồn trả lời một câu hỏi khác nhau

1. **`pg_test_fsync`** — chạy trong container, trên chính data directory: một commit tốn bao lâu. (ext4 chạy native nên không có số này; ZFS nhận O_DIRECT nên đo được.)
2. **`pg_stat_io`** — chính PostgreSQL báo cáo thời gian từng syscall (`write_time`, `fsync_time`, `writeback_time`, `read_time`) trong suốt lần chạy đó. Không phải microbenchmark, mà là số đo từ đúng process sinh ra workload.

Executor là closed-loop, nên throughput bị chặn bởi thành phần chậm nhất chứ không phải bởi request generator. Số tuyệt đối dao động ±40% giữa các lần chạy trên runner dùng chung; tôi chỉ so các ratio đo trong cùng một lần chạy, trừ khi nói rõ là so giữa các đợt.

## 1. Sharding vs non-sharding: phần ghi

Kỳ vọng trước khi đo: chia bảng ra 32 phần thì ghi song song tốt hơn. Kết quả ở 10M rows, k6 closed loop, p95 trong ngoặc:

| Model (connections) | ext4 | XFS | btrfs | ZFS |
|---|---|---|---|---|
| M0 — 1 bảng (16) | 6 157 (3 ms) | 4 793 (5 ms) | 4 484 (6 ms) | **8 862 (3 ms)** |
| M1-P8 — 128 | 5 028 (82 ms) | 4 243 (75 ms) | 4 239 (72 ms) | 7 230 (60 ms) |
| M1-P32 — 500 | 5 481 (189 ms) | 3 975 (227 ms) | 4 007 (232 ms) | 6 577 (160 ms) |
| M2 — 32 bảng (16) | **6 757** (4 ms) | 4 571 (6 ms) | 4 283 (6 ms) | **8 915** (3 ms) |

| Model | ext4 | XFS | btrfs | ZFS |
|---|---|---|---|---|
| **M0** (baseline) | 6 157 | 4 793 | 4 484 | 8 862 |
| **M2** — 32 bảng | 6 757 (+9.7%) | 4 571 (−4.6%) | 4 283 (−4.5%) | 8 915 (+0.6%) |
| **M1-P8** — 128 | 5 028 (−18.3%) | 4 243 (−11.5%) | 4 239 (−5.5%) | 7 230 (−18.4%) |
| **M1-P32** — 500 | 5 481 (−11.0%) | 3 975 (−17.1%) | 4 007 (−10.6%) | 6 577 (−25.8%) |

*Rows/giây ở 10M rows; phần trăm là thay đổi so với M0 của cùng filesystem.*

Hai điều đáng chú ý:

- **M2 so với M0 gần như phẳng.** Trên ext4, M2 nhỉnh hơn 10% (6 757 so với 6 157) và p95 tương đương; trên ZFS +0.6%. 32 heap và 32 btree root thay đổi page contention một chút, nhưng không thay đổi cổng commit.
- **M1 thua M0 dù có nhiều connection hơn 8–31 lần:** thấp hơn 5–26% tuỳ FS, p95 tệ hơn 10–60×. Pool-per-schema chia namespace, relcache và pool; nó không chia commit path. Thêm pool trên một instance nghĩa là thêm backend xếp hàng trên cùng hai lock.

Lý do nằm trong source, không phải trong cảm nhận: mỗi commit kết thúc ở `XLogFlush` (`xlog.c:2780`), nơi lấy `WALWriteLock` duy nhất của instance; `ProcArrayEndTransaction` (`procarray.c:667`) lấy `ProcArrayLock` độc quyền mỗi commit. Cả hai lock đều không biết tên bảng. Chia một bảng thành 32 bảng cho bạn 32 heap, 32 btree root — và đúng một fsync lane.

> **Note — đọc từ source qua MCP codegraph.** Mình bind cây source PostgreSQL tại `sources/postgres` vào codegraph MCP (`codegraph_init`, `index=false`) rồi lần theo call graph; các dòng dưới đây đã đối chiếu lại bằng grep trực tiếp trên source. `CommitTransaction` (`xact.c:2228`) gọi `RecordTransactionCommit()` (`xact.c:2365`), hàm này kết thúc ở `XLogFlush(XactLastRecEnd)` (`xact.c:1502`). Trong `XLogFlush` (`xlog.c:2780`), việc flush bị chặn bởi đúng một `WALWriteLock`; dòng 2854 dùng `LWLockAcquireOrWait(WALWriteLock, LW_EXCLUSIVE)`, nên một backend làm leader và các backend còn lại chờ kết quả thay vì tự flush — comment ngay trên nói rõ mục đích là giữ "a good rate of group committing". `codegraph_callers(XLogFlush)` trả về 27 caller (commit, checkpoint, buffer flush, walsender...), không caller nào biết bảng đích. Song song, `CommitTransaction` gọi `ProcArrayEndTransaction` (`xact.c:2389`), hàm lấy `ProcArrayLock` độc quyền (`procarray.c:667`; các nhánh ở dòng 684 và 716). Không lock nào trong hai nhìn thấy tên bảng — nên chia bảng không bỏ được gì khỏi đường này, chỉ thêm người xếp hàng sau cùng một leader. Các số dòng trên đúng cho PostgreSQL 18.6, cây source ở `sources/postgres`; container benchmark chạy image `latest` và tôi không ghi lại digest, nên không có version server chính xác để trích.

Ở 50M và 100M, tôi chạy toàn bộ sweep lần thứ hai. Bảng dưới không phải để xếp hạng filesystem mà để cảnh báo về nhiễu:

| FS | đợt | 50M, 1 bảng | 50M, 32 bảng | 100M, 1 bảng | 100M, 32 bảng |
|---|---|---|---|---|---|
| ext4 | 1 | 7 168 | 6 659 | 6 854 | 6 612 |
| ext4 | 2 | 4 898 | 4 869 | 5 006 | 5 047 |
| XFS | 1 | 4 632 | 4 519 | 4 523 | 4 568 |
| XFS | 2 | 5 218 | 5 880 | 5 757 | 5 089 |
| btrfs | 1 | 4 448 | 4 328 | 4 978 | 5 106 |
| btrfs | 2 | 8 656 | 8 739 | 8 899 | 8 473 |
| ZFS (`recordsize=8k`) | 2 | **8 909** | **8 895** | chưa đo | chưa đo |

Điều sống sót qua cả hai đợt: **1 bảng so với 32 bảng chênh tối đa khoảng 13%, và chênh đi cả hai chiều.** Mức 13% đó là XFS đợt 2 ở 50M (5 218 so với 5 880) — khoảng cách lớn nhất trong bảng, vẫn nhỏ hơn mức dao động 1.95× giữa hai đợt của cùng một filesystem; mọi dòng còn lại còn sát hơn. Sharding trên một instance không mua được write throughput, và bảng số liệu này nói đúng như vậy ở cả 50M lẫn 100M.

## 2. Sharding vs non-sharding: phần đọc

Lần đo đọc đầu tiên cho một khoảng cách bốn bậc độ lớn: view `UNION ALL` 32 nhánh mất 4.9–19.9 s, còn route thẳng vào một shard chỉ 0.11–0.20 ms. Tôi đã công bố khoảng cách đó trước khi tìm ra nguyên nhân, và nguyên nhân nằm ở harness, không phải sharding. Lần chạy chỉ đọc không insert nên autoanalyze không chạy, và planner coi bảng 10M rows là rỗng; `random()` trong `WHERE` là volatile (`is_pseudo_constant_clause`, `clauses.c:2113`) nên không index nào dùng được, mọi "point lookup" thành seq scan với `LIMIT 1`. Các shape `routed_*` lại dùng literal id, nên phép so sánh thực chất là một index lookup với 32 seq scan. Sửa cả hai — `ANALYZE`, và đưa randomisation về client — cho ra số dưới đây.

Cả hai đường đọc dùng chung 32 bảng. Mỗi bảng giữ một shard, cùng cột và cùng index:

```sql
CREATE TABLE users_XX (              -- XX = 00 … 31
    user_id    bigint      PRIMARY KEY,
    email      citext      NOT NULL,
    name       text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON users_XX (email);
CREATE INDEX        ON users_XX (created_at);

CREATE VIEW users_all AS
    SELECT * FROM users_00
    UNION ALL SELECT * FROM users_01
    -- … 30 nhánh nữa …
    UNION ALL SELECT * FROM users_31;
```

Đường **view** gửi câu query vào `users_all`, planner bung ra cả 32 nhánh. Đường **routed** băm shard ở client rồi gửi đúng câu query đó vào một bảng. Harness chèn thẳng id dạng literal, không dùng bind parameter. Ví dụ dưới đây dùng một id giữa dải dữ liệu, `5 000 000`, rơi vào `users_00`:

```sql
-- view: một câu query, 32 nhánh trong plan
SELECT user_id, name FROM users_all
 WHERE email = 'user5000000@example.com' LIMIT 1;

-- routed: 5000000 % 32 = 0, nên chỉ một bảng
SELECT user_id, name FROM users_00
 WHERE email = 'user5000000@example.com' LIMIT 1;
```

`by_id` là cặp tương tự, thay predicate email bằng `WHERE user_id = 5000000`. Hai bên dùng cùng unique index trên email và cùng primary key, nên khác biệt duy nhất là `Append` 32 nhánh.

50M rows, 32 bảng, 16 connections, p50 / p95 (ms), cache ấm:

| FS | view `login` | routed `login` | view `by_id` | routed `by_id` |
|---|---|---|---|---|
| ext4 | 0.26 / 9.3 | 0.18 / 1.3 | 0.25 / 2.3 | 0.17 / 0.8 |
| XFS | 0.17 / 7.2 | 0.15 / 1.1 | 0.17 / 1.6 | 0.13 / 0.9 |
| btrfs | 0.11 / 6.2 | 0.07 / 1.2 | 0.11 / 1.3 | 0.06 / 0.7 |
| ZFS | 0.11 / 30.9 | 0.07 / 1.9 | 0.10 / 1.7 | 0.07 / 1.2 |

<svg role="img" aria-labelledby="chart-reads-vi-title chart-reads-vi-desc" viewBox="0 0 640 240" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-reads-vi-title">p95 warm: view 32 nhánh so với read được route</title>
  <desc id="chart-reads-vi-desc">Biểu đồ cột theo nhóm, p95 warm cho query login ở 50 triệu rows. ext4: 9.3 mili-giây qua view và 1.3 khi route. XFS: 7.2 và 1.1. btrfs: 6.2 và 1.2. ZFS: 30.9 và 1.9.</desc>
  <g font-family="system-ui, sans-serif" font-size="12" fill="currentColor">
    <line x1="60" y1="190" x2="610" y2="190" stroke="currentColor" stroke-opacity="0.25"/>
    <rect x="82" y="141" width="34" height="49" fill="#ef4444" rx="2"/>
    <rect x="120" y="183" width="34" height="7" fill="#10b981" rx="2"/>
    <text x="99" y="133" text-anchor="middle" font-size="10">9.3</text>
    <text x="137" y="179" text-anchor="middle" font-size="10">1.3</text>
    <text x="118" y="208" text-anchor="middle">ext4</text>
    <rect x="222" y="154" width="34" height="36" fill="#ef4444" rx="2"/>
    <rect x="260" y="184" width="34" height="6" fill="#10b981" rx="2"/>
    <text x="239" y="146" text-anchor="middle" font-size="10">7.2</text>
    <text x="277" y="180" text-anchor="middle" font-size="10">1.1</text>
    <text x="258" y="208" text-anchor="middle">XFS</text>
    <rect x="362" y="159" width="34" height="31" fill="#ef4444" rx="2"/>
    <rect x="400" y="184" width="34" height="6" fill="#10b981" rx="2"/>
    <text x="379" y="151" text-anchor="middle" font-size="10">6.2</text>
    <text x="417" y="180" text-anchor="middle" font-size="10">1.2</text>
    <text x="398" y="208" text-anchor="middle">btrfs</text>
    <rect x="502" y="35" width="34" height="155" fill="#ef4444" rx="2"/>
    <rect x="540" y="181" width="34" height="9" fill="#10b981" rx="2"/>
    <text x="519" y="27" text-anchor="middle" font-size="10">30.9</text>
    <text x="557" y="177" text-anchor="middle" font-size="10">1.9</text>
    <text x="538" y="208" text-anchor="middle">ZFS</text>
    <rect x="60" y="16" width="10" height="10" fill="#ef4444" rx="1"/>
    <text x="76" y="25" font-size="11">view 32 nhánh</text>
    <rect x="180" y="16" width="10" height="10" fill="#10b981" rx="1"/>
    <text x="196" y="25" font-size="11">read được route</text>
    <text x="60" y="232" font-size="11" fill-opacity="0.7">p95, ms, cache ấm, 50M rows</text>
  </g>
</svg>

Cold cache (định nghĩa ở Setup; ext4 không có dòng cold vì chạy native, không qua loop device mà pha cold cần):

| Cold, 50M, p50 (ms) | view `login` | routed `login` |
|---|---|---|
| XFS | 132.8 | 1.11 |
| btrfs | 78.6 | 1.40 |
| ZFS | 69.0 | 1.77 |

Cách đọc bảng này:

- **Warm p50: route gần như không đổi.** View 32 nhánh chỉ cách routed 0.04–0.08 ms trên mọi FS. Append dừng ở nhánh đầu tiên có row, và với statistics mỗi nhánh được một index scan. Chi phí là một plan node, không phải 32 lần quét.
- **Warm p95: route hơn 5–16×.** 1.1–1.9 ms so với 6.2–30.9 ms. Đuôi là nhánh không khớp, cộng thêm ARC trên ZFS.
- **Cold: route hơn 39–120×.** 0.7–1.8 ms so với 69–133 ms, vì view chạm 32 index root thay vì một.
- **Đó là fan-out, không phải index to hơn.** Index của mỗi shard nhỏ đi khoảng 32 lần, tổng dung lượng gần như không đổi. Thứ thay đổi là số index root một query phải chạm: một khi routed, 32 khi qua view. Khi cold, đó là 32 random read thay vì một.
- **Routing giả định caller biết shard.** Chân routed biết vì harness tự tính `uid % 32`. Login bằng email thì không: app không biết email đó nằm shard nào, nên cần bảng tra (`email → user_id`) hoặc shard key trùng với key mà query dùng.
- **Cold không chỉ là chuyện restart.** Khi working set không còn nằm trong RAM, một phần read ngày nào cũng hành xử như dòng cold. Ở 50M rows trên box 8 GB, đó là trạng thái thường trực chứ không phải ngoại lệ — nên routing đáng làm ở size đó, và không đáng ở 10M khi bảng còn nằm trong cache.
- **Routed không tăng từ 50M lên 100M** (ext4 0.18→0.18, XFS 0.15→0.13, btrfs 0.07→0.08).

Bài học thực dụng: **một view `UNION ALL` trên các shard rất rẻ khi planner có statistics và predicate dùng được index; nó thảm hoạ khi không.** Trước khi viết lại application để route, hãy chạy `EXPLAIN (ANALYZE, BUFFERS)` trên đúng query đó. Nếu kế hoạch đang là 32 seq scan, vấn đề là statistics hoặc shape, không phải sharding.

## 3. Filesystem: bốn cái, và một cuộc so sánh không thể phân giải

Cùng scenario, cùng filesystem, cùng commit — hai đợt đo cách nhau vài giờ. Một "đợt" ở đây là một lần chạy trọn bộ: mọi filesystem và cả hai layout bảng, trong một phiên trên một runner; giữa hai đợt, runner và ổ đĩa backing đều đổi.

| FS | 50M, đợt 1 | 50M, đợt 2 | thay đổi |
|---|---|---|---|
| ext4 | 7 168 | 4 898 | −32% |
| XFS | 4 632 | 5 218 | +13% |
| btrfs | 4 448 | 8 656 | +95% |
| ZFS | — | 8 909 | — |

<svg role="img" aria-labelledby="chart-noise-vi-title chart-noise-vi-desc" viewBox="0 0 640 230" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-noise-vi-title">Dao động giữa hai đợt đo trên cùng một filesystem</title>
  <desc id="chart-noise-vi-desc">Biểu đồ cột theo nhóm, throughput ghi 50 triệu rows trong hai đợt. ext4: 7 168 rows/giây rồi 4 898. XFS: 4 632 rồi 5 218. btrfs: 4 448 rồi 8 656. Cùng một phép thử dao động tới 1.95 lần giữa hai đợt.</desc>
  <g font-family="system-ui, sans-serif" font-size="12" fill="currentColor">
    <line x1="60" y1="185" x2="610" y2="185" stroke="currentColor" stroke-opacity="0.25"/>
    <rect x="105" y="53" width="50" height="132" fill="#3b82f6" rx="2"/>
    <rect x="160" y="95" width="50" height="90" fill="#f59e0b" rx="2"/>
    <text x="130" y="45" text-anchor="middle" font-size="10">7 168</text>
    <text x="185" y="87" text-anchor="middle" font-size="10">4 898</text>
    <text x="157" y="203" text-anchor="middle">ext4</text>
    <rect x="285" y="100" width="50" height="85" fill="#3b82f6" rx="2"/>
    <rect x="340" y="89" width="50" height="96" fill="#f59e0b" rx="2"/>
    <text x="310" y="92" text-anchor="middle" font-size="10">4 632</text>
    <text x="365" y="81" text-anchor="middle" font-size="10">5 218</text>
    <text x="337" y="203" text-anchor="middle">XFS</text>
    <rect x="465" y="103" width="50" height="82" fill="#3b82f6" rx="2"/>
    <rect x="520" y="25" width="50" height="160" fill="#f59e0b" rx="2"/>
    <text x="490" y="95" text-anchor="middle" font-size="10">4 448</text>
    <text x="545" y="17" text-anchor="middle" font-size="10">8 656</text>
    <text x="517" y="203" text-anchor="middle">btrfs</text>
    <rect x="60" y="12" width="10" height="10" fill="#3b82f6" rx="1"/>
    <text x="76" y="21" font-size="11">đợt 1</text>
    <rect x="150" y="12" width="10" height="10" fill="#f59e0b" rx="1"/>
    <text x="166" y="21" font-size="11">đợt 2</text>
    <text x="60" y="224" font-size="11" fill-opacity="0.7">rows/giây, 50M rows, một bảng</text>
  </g>
</svg>

Seed time đổi theo (ext4 950 s → 1 137 s cho cùng 50M rows; btrfs 872 s → 628 s). Đó là dấu hiệu: mỗi lần chạy nhận một runner mới với ổ backing khác nhau. **Chênh lệch giữa các FS (1.4–1.9×) không thể phân giải bằng một lần chạy cho mỗi ô khi bản thân phép đo dao động tới 1.95×.**

Vậy còn lại gì?

- Cả bốn filesystem nằm trong khoảng ~2× của nhau. Không cái nào nhanh hơn 10×, không cái nào là "sai lầm".
- Trong đợt 2, ZFS dẫn đầu (8 909) và btrfs gần bằng (8 656), XFS 5 218, ext4 4 898. Trong đợt 1, ext4 dẫn đầu. **Không có thứ hạng ổn định.**
- ZFS là FS duy nhất cho kết quả gần như trùng nhau ở cả bốn lần chạy (8 862 và 8 915 ở 10M, 8 909 và 8 895 ở 50M) — nhưng bốn điểm vẫn không phải bằng chứng, vì chúng đến từ các runner khác nhau, và cặp 10M đo ở `recordsize=128K` còn cặp 50M đo ở 8k.

### Build index: chỗ filesystem lộ ra rõ hơn

Không có concurrency che, nên thời gian build sạch hơn:

| Index (10M rows) | build ext4 | build XFS | build btrfs | build ZFS (128K) | build ZFS (8k) |
|---|---|---|---|---|---|
| BRIN trên `created_at` | 5.6 s | 5.5 s | 5.6 s | 16.7 s | 19.4 s |
| GiST trên `point` | 10.2 s | 10.5 s | 12.1 s | 35.9 s | 24.7 s |
| btree trên `name` | 24.6 s | 25.8 s | 25.7 s | 42.2 s | 31.1 s |
| GIN trigram trên `name` | 15.6 s | 15.8 s | 17.5 s | 33.2 s | 19.9 s |

ext4, XFS và btrfs build các index này trong khoảng 20% của nhau (GiST là rộng nhất: 10.2 s so với 12.1 s trên btrfs); filesystem chỉ lộ ra trên ZFS, nơi record 128K chưa tune trả 1.7–3.5× thời gian build của ext4 cho cùng số byte. Record 8 KB thu hẹp phần lớn khoảng cách với các index nặng metadata (btree 1.3× ext4, GIN 1.3×, GiST vẫn 2.4×) — nhưng đây là một lần chạy khác, nên chỉ đọc như dấu hiệu. BRIN giữ 3.0–3.5× ext4 ở cả hai lần, vì đó là chi phí cố định, gần như không có I/O để amortise. Không phải "ZFS build chậm"; mà là "ZFS build chậm với AM và record size bạn chọn".

### fsync microbenchmark không dự đoán được throughput

`pg_test_fsync`, một lần ghi 8 KB + fsync:

| FS | fsync 1×8KB | pg_stat_io fsync trung bình trong 90 giây của lần chạy | throughput ghi (đợt 2) |
|---|---|---|---|
| XFS | 393 µs | 3.7 ms | 5 218 rows/s |
| btrfs | 561 µs | 3.5 ms | 8 656 rows/s |
| ZFS | 210 µs | 2.3 ms | 8 909 rows/s |
| ext4 | không đo được (native) | không đo được | 4 898 rows/s |

Nếu fsync quyết định tất cả, btrfs phải chậm nhất. Nó lại nhanh nhất trong đợt 2. Lý do là group commit: số commit được gộp vào một lần fsync khác nhau giữa các FS. Vì vậy một microbenchmark fsync — dù là của chính PostgreSQL — không đủ để chọn filesystem cho workload commit nhỏ. Phải đo trên workload thật.

### ZFS: recordsize là một cái bẫy

Dataset ZFS mặc định `recordsize=128K`. Postgres ghi page 8 KB; mỗi page bẩn buộc ZFS đọc-sửa-ghi cả record 128 KB — khuếch đại tới 16× khi working set không còn nằm trong ARC. Hệ quả đo được: lần chạy 50M/100M chết sau bốn giờ vì runner hết đĩa (`No space left on device`), đến file log của runner cũng không ghi nổi.

`recordsize=8k` sửa được vụ đầy đĩa, và **không** tạo ra throughput: matrix 10M đo 8 862 rows/s ở 128K, lần chạy 50M đo 8 909 ở 8k. Nó chỉ giúp cùng lượng dữ liệu đó vừa runner. Mọi số ZFS đo trước bản sửa đó đều là chưa tune.

## 4. Nhân tố thứ ba: loại index

Cùng bảng 10M rows, mỗi lần chạy thêm một index. Bảng dưới là toàn bộ lập luận cho "chọn index theo query, không theo cột":

| Shape | Loại thắng | p50 / p95 | Không index |
|---|---|---|---|
| Range 7 ngày trên `created_at` | **BRIN** | 1.7 / 4.7 ms | 3 086 / 3 130 ms |
| Equality trên `rating` (~100 rows) | **hash** | 0.23 / 1.0 ms | 472 / 519 ms |
| kNN, `ORDER BY loc <-> point` | **GiST** | 0.41 / 1.24 ms | 1 117 / 1 172 ms |
| Prefix `LIKE 'User 1234%'` | **SP-GiST** | 0.22 / 0.83 ms | 0.30 / 638 ms |
| Substring `LIKE '%12345%'` | GIN (ổn định 4.2 ms) | 4.2 / 4.7 ms | 0.24 / 96 ms |

Các số p50/p95 ở trên đo trên XFS; trên ba filesystem còn lại, cùng shape đó nằm trong ±40%. Kích thước index thì giống nhau trên cả bốn FS, vì đó là dữ liệu, không phải cách bố trí.

| Index | Kích thước ở 10M rows |
|---|---|
| BRIN | **56 KB** |
| GIN (trigram) | 178.6 MB |
| btree | 300.8 MB |
| hash | 306.6 MB |
| SP-GiST | 337–429 MB |
| GiST | 510.8 MB |

BRIN trên `created_at` nhỏ hơn index GiST trên `point` hơn 9.000 lần (56 KB so với 511 MB), vì nó lưu một summary theo block range thay vì một entry cho từng row. Nó cũng gần như miễn phí khi maintain: nằm trong nhiễu so với lần chạy không index trên ext4 và btrfs.

Thuế ghi của các loại còn lại là thật nhưng nhỏ hơn tôi tưởng — và phụ thuộc filesystem:

| Index | ext4 | XFS | ZFS (128K) | ZFS (8k) |
|---|---|---|---|---|
| BRIN | −0.6% | −1.1% | −4.4% | +0.3% |
| btree | −1.3% | −3.4% | +6.1% | −0.5% |
| GIN | −1.5% | −4.7% | −9.1% | −1.4% |
| SP-GiST (point) | −3.8% | −11.2% | −20.1% | −4.4% |
| GiST (point) | −3.5% | −6.2% | −13.3% | −9.5% |

Trên ext4, ngay cả access method nặng nhất cũng mất dưới 4% insert throughput — transaction nào cũng phải chờ một lần WAL flush. Trên ZFS 128K, index maintenance rơi vào cùng cửa sổ đó và tốn tới 20%; ở 8k còn 9.5%. "Index tốn kém khi ghi" là đúng, nhưng độ lớn của hoá đơn là thuộc tính của cả stack.

Hai caveat tôi không che:

- Query prefix thắng nhờ SP-GiST **chỉ vì** biến thể btree được định nghĩa là `CREATE INDEX ON docs (name)`. Với non-C collation, btree thường không phục vụ được `LIKE 'prefix%'`; nó rơi về scan (663 ms p95). Có `text_pattern_ops` thì nó cạnh tranh được. Đó là chi tiết định nghĩa index, không phải phán quyết về access method.
- Query substring **không** chứng minh GIN nhanh hơn. Nó có `LIMIT 100` và bảng sắp theo id, nên seq scan tìm đủ 100 match đầu sau ~13k rows rồi thoát trong 0.2 ms; GIN trả 4.2 ms cố định. GIN là biến thể duy nhất ổn định, nhưng "ổn định" không phải "nhanh hơn".

## 5. Bao nhiêu connection là "đủ"? Hai điểm gãy khác nhau

**Ghi**, từ matrix phía trên:

| connections | throughput | p95 |
|---|---|---|
| 16 | 6 157–8 862 | 3–6 ms |
| 128 | 5 028–7 230 | 60–82 ms |
| 500 | 5 481–6 577 | 160–232 ms |

Throughput ngừng tăng rồi giảm; latency tăng gần như theo độ dài queue. Cơ chế là đoàn tàu fsync: một `WALWriteLock`, mỗi lúc một flush leader, mỗi backend thêm vào là một khách nữa trong cùng hàng đợi. Quá ~16 backend trên bốn core, bạn không mua parallelism — bạn mua thời gian chờ.

**Read**, point lookup với 1/4/16/64/256 connections:

| connections | throughput | p50 | p95 |
|---|---|---|---|
| 1 | 5 165 q/s | 0 ms | 1 ms |
| 4 | 9 304 q/s | 0 ms | 1 ms |
| **16** | **10 060 q/s** | 1 ms | 3 ms |
| 64 | 9 484 q/s | 4 ms | 35 ms |
| 256 | 7 439 q/s | 28 ms | 78 ms |

Read scale được — gấp đôi từ 1 lên 16 — vì không xếp hàng trên WAL flush. Chúng xếp hàng trên CPU. Knee ở 16 connections cho 4 vCPU, tức `cores × 2–4`; quá đó throughput giảm 26% và p95 tăng 26×.

<svg role="img" aria-labelledby="chart-knee-title chart-knee-desc" viewBox="0 0 620 220" xmlns="http://www.w3.org/2000/svg" style="max-width:100%; height:auto; background:transparent;">
  <title id="chart-knee-title">Throughput point-read theo số connection</title>
  <desc id="chart-knee-desc">Biểu đồ cột throughput point-read: 5.165 queries/giây ở 1 connection, 9.304 ở 4, 10.060 ở 16, 9.484 ở 64, 7.439 ở 256.</desc>
  <g font-family="system-ui, sans-serif" font-size="12" fill="currentColor">
    <line x1="80" y1="180" x2="590" y2="180" stroke="currentColor" stroke-opacity="0.25"/>
    <rect x="100" y="107" width="60" height="73" fill="#3b82f6" rx="2"/>
    <text x="130" y="99" text-anchor="middle">5 165</text>
    <text x="130" y="198" text-anchor="middle">1</text>
    <rect x="190" y="49" width="60" height="131" fill="#3b82f6" rx="2"/>
    <text x="220" y="41" text-anchor="middle">9 304</text>
    <text x="220" y="198" text-anchor="middle">4</text>
    <rect x="280" y="38" width="60" height="142" fill="#10b981" rx="2"/>
    <text x="310" y="30" text-anchor="middle">10 060</text>
    <text x="310" y="198" text-anchor="middle">16</text>
    <rect x="370" y="46" width="60" height="134" fill="#f59e0b" rx="2"/>
    <text x="400" y="38" text-anchor="middle">9 484</text>
    <text x="400" y="198" text-anchor="middle">64</text>
    <rect x="460" y="75" width="60" height="105" fill="#ef4444" rx="2"/>
    <text x="490" y="67" text-anchor="middle">7 439</text>
    <text x="490" y="198" text-anchor="middle">256</text>
    <text x="80" y="24" font-size="13">queries/giây, point lookup 10M rows, 4 vCPU</text>
  </g>
</svg>

Quy tắc thực dụng: **size pool theo knee, không theo số application server.** Nếu cần write capacity hơn fsync lane của một instance, đáp án là thêm instance — replica cho read, shard cho write — không phải thêm 100 connections.

## 6. Vậy XFS có thật sự nhanh hơn ext4?

Đây là câu hỏi khởi đầu của cả bài. Sau tất cả số liệu này, câu trả lời trung thực là: **không có bằng chứng XFS nhanh hơn ext4 trong workload này, và cũng không có bằng chứng ngược lại.** Cả bốn filesystem nằm trong ~2× của nhau, và độ dao động giữa hai đợt đo lớn hơn khoảng cách giữa chúng.

Điều XFS có thể đúng trong thực tế — nhưng bài này không đo được:

- Hành vi với file lớn và workload nhiều writer song song ở quy mô lớn hơn 4 vCPU.
- `xfs_growfs`/online resize, reflink, và các tính năng vận hành.
- Độ ổn định dưới tải thật, trong nhiều giờ, chứ không phải 90 giây.

Những gì bài này đo được, và có thể dùng để quyết định:

1. **Đừng chọn filesystem vì một bảng benchmark fsync.** fsync đơn lẻ không dự đoán throughput; group commit và commit path mới quyết định.
2. **Đừng sharding trên một instance để tìm write throughput.** Nó không có ở đó. Sharding chỉ đáng làm khi (a) bạn route được read, hoặc (b) bạn tách sang instance khác.
3. **Đừng chọn filesystem vì point read.** Khác biệt là phần mười ms khi warm; routing mới là thứ tạo ra 39–120× khi cold.
4. **ZFS chỉ dùng khi đã set `recordsize=8k`.** Mặc định 128K với Postgres là một cái bẫy đầy đĩa, không phải một lựa chọn hiệu năng.

Nếu buộc phải chọn hôm nay: **ext4 hoặc XFS đều là mặc định hợp lý**; ZFS chỉ khi team đã vận hành quen và biết tune; btrfs không có lợi thế ổn định nào trong workload này.

| Triệu chứng | Kiểm tra đầu tiên |
|---|---|
| Point lookup chậm dần khi dữ liệu lớn | Query có được route không, hay đang scan cả view `UNION`? Chạy `EXPLAIN (ANALYZE, BUFFERS)`. |
| Ghi chậm khi tải cao, latency lên xuống thất thường | Số connection so với fsync lane; xem p95, đừng xem trung bình. |
| Một query dashboard chậm | Loại index so với shape (range → BRIN, kNN → GiST, equality → hash). |
| Mọi thứ chậm 20–40% sau khi migrate | Hành vi commit của filesystem — cùng số byte, hoá đơn khác. |
| Đĩa tăng nhanh hơn số row | Dung lượng index: 56 KB so với 511 MB trên cùng một bảng. |
| ZFS đầy đĩa sau vài giờ | `recordsize` của dataset có phải 128K? Postgres ghi page 8 KB. |

## Cấu hình minimum viable production

Vậy backend của cậu sinh viên đó có chịu được traffic thật không? Ở size này, có — với setup sau:

| Hạng mục | Thiết lập | Vì sao |
|---|---|---|
| Filesystem | ext4, hoặc XFS nếu host đã dùng sẵn | chênh ≤2×, dưới mức nhiễu giữa hai đợt |
| `shared_buffers` | 1 GB | giá trị đã chạy; nút thắt của read là CPU, không phải buffer |
| Pool | một pool, ~16 connections (4 vCPU × 4) | write p95 3 ms ở 16, 82 ms ở 128, 189 ms ở 500 (ext4); read knee cũng ở 16 |
| Bảng | một bảng `users` | write chênh ≤13% cả hai chiều; khi dữ liệu còn trong cache, view đo được 0.26 ms p50 / 9.3 ms p95 (ext4, 50M), nên tách bảng chỉ mua latency sau khi working set vượt RAM — và phải trả bằng JOIN xuyên shard, transaction xuyên shard và unique email toàn cục |
| Index | btree cho equality/prefix, BRIN cho range thời gian, GiST cho kNN, GIN chỉ cho substring | tới 1.800× với đúng shape; chi phí ghi dưới 4% trên ext4 |
| Routing | chưa cần, và không cần nếu chỉ một bảng | một bảng thì không có shard để route. Nếu đã tách, route khi working set vượt RAM: cold p50 từ 69–133 ms xuống 1.1–1.8 ms |
| Sau khi seed | `ANALYZE` | thiếu nó planner coi bảng là rỗng |
| ZFS (nếu buộc phải dùng) | `recordsize=8k` | 128K làm đầy đĩa sau bốn giờ |

Hết. Những nút mọi người hay tranh cãi — filesystem, số shard — thay đổi ít nhất. Những nút không ai tranh cãi — index theo query shape, statistics, pool theo box — thay đổi nhiều nhất. Không có gì ở đây phụ thuộc version Postgres, trừ số dòng source ở mục 1.

**Một lưu ý về ZFS.** Nó đứng đầu bảng ghi (8 862 và 8 915 rows/s ở 10M, 8 909 và 8 895 ở 50M) và là filesystem ổn định nhất giữa các lần chạy, nên không thể nói nó chậm. Điều có thể nói: mình **chưa tìm được cách tối ưu ZFS cho cấu hình này** để dám lấy nó làm mặc định. Liệt kê những gì còn mở, thay vì kết luận vội:

- **Recordsize.** Mặc định `recordsize=128K` làm đầy đĩa sau bốn giờ. `8k` sửa được, nhưng chưa chạy 100M với nó, nên ưu thế ghi mới chỉ được đo ở 10M và 50M.
- **Build index.** 1.7–3.5× ext4 ở 128K, vẫn 1.3–3.5× ở 8k. Chưa thử recordsize khác hay `logbias` để xem có thu hẹp được không.
- **Cold read.** `drop_caches` không xoá ARC, cùng một query cho 69 ms và 743 ms. Chưa tìm được cách ép cold read thật trên ZFS trong harness này.
- **ARC trên 8 GB.** Chưa đo ARC lấy bao nhiêu RAM của `shared_buffers` và workload, nên đánh đổi bộ nhớ vẫn là ẩn số.
- **Warm p95.** 30.9 ms so với 9.3 ms trên ext4, và chưa biết đó là ARC eviction hay thứ gì tune được.

Nên kết luận là "mình chưa đưa được ZFS về trạng thái đủ an toàn để giao cho một project sinh viên", không phải "ZFS dở". Nếu team đã chạy ZFS và tune quen thì ổn; còn với box production đầu tiên, ext4 hoặc XFS nằm cùng dải ghi với ít ẩn số hơn.

## Khi nào nên sharding, khi nào không

Bài này đo table sharding **trong một instance** (M2). Sharding **qua nhiều instance** là chuyện khác, và bài không đo. Chính khác biệt đó quyết định phần lớn lời khuyên:

- **Trong một instance**, tách bảng không mua được gì cho ghi: cùng WAL, cùng một lần flush commit, một `WALWriteLock` cho mỗi instance. Chênh ±13%, cả hai chiều.
- **Qua nhiều instance**, mỗi instance có fsync lane riêng, nên write capacity scale được — đổi lại là JOIN xuyên shard, transaction xuyên shard, unique toàn cục và rebalancing. Nằm ngoài phạm vi bài này.

Nên quy tắc không phải "đừng bao giờ shard", mà là:

| Tình huống | Shard? |
|---|---|
| Muốn thêm write throughput | Không bằng cách tách bảng trong một instance — thêm instance |
| Working set vẫn nằm trong RAM và p95 ổn | Không: view chỉ tốn dưới 0.1 ms ở p50 |
| Dữ liệu đã vượt RAM (50M+ trên box này) và cold p50 quan trọng | Có — tách bảng, rồi route theo key mà query dùng |
| Một bảng quá lớn để build index, vacuum, backup hay `ALTER` trong thời gian hợp lý | Có, hash hoặc theo thời gian — lý do vận hành, không phải latency |
| Query không mang shard key (search toàn bộ, JOIN xuyên shard) | Không, hoặc chấp nhận view — chỉ ổn khi dữ liệu còn trong cache |
| "Mỗi tenant/schema một pool" (M1) | Không: throughput giảm 5–26%, p95 tệ tới 60× |

Routing có điều kiện tiên quyết: **application phải biết shard trước khi gửi query.** `by_id` thỏa điều đó một cách tự nhiên. Login bằng email thì không, nên cần bảng tra hoặc shard key trùng với key query. Con số routed trong bài chỉ đạt được khi điều kiện đó đúng.

Thứ tự làm, từ tác động lớn đến nhỏ:

1. Fix query và index (tới 1.800×).
2. Size pool theo knee (~16 trên 4 vCPU).
3. Thêm RAM khi còn hiệu quả; read replica là bước tiếp theo thường gặp, nhưng bài này không đo.
4. Sharding, kèm routing, khi một bảng đã thành vấn đề vận hành *và* bạn cần tail latency thấp.

## Hai cái bẫy đo khác

Hai cái nữa từng làm sai kết quả, và cả hai đều dễ lặp lại:

1. **Cast `citext` không khớp index.** `citext → text` là IMPLICIT, `text → citext` chỉ ASSIGNMENT, nên `email = <biểu thức text>` so sánh như text và bỏ qua citext index. Một `::citext` đổi 4.6 s scan thành 0.2 ms.
2. **Benchmark scan không scan gì.** `now() - interval '1 day'` khớp 13k trong 10M rows; `count(*)` được trả lời bằng index-only scan. Không shape nào lộ ra parallel plan. Sửa thành 365 ngày + `sum(user_id)` — và kết quả worker vẫn không kết luận được, tôi báo cáo đúng như vậy.

## Giới hạn

- 10M rows cho index lab, 50M/100M cho sweep sharding. Mức 200M bị bỏ vì ~80 GB vượt đĩa runner.
- 4 vCPU, 8 GB, một container mỗi lần chạy. Throughput tuyệt đối dao động ±40%; riêng sweep scale dao động tới 1.95× giữa hai đợt, nên **không xếp hạng được filesystem** từ dữ liệu này.
- ZFS chỉ đo ở 50M, với `recordsize=8k`. Lần chạy 100M chưa đo.
- ext4 chạy native (không loop device) nên không có cold read, `pg_test_fsync` hay `pg_stat_io` cho ext4.
- **f2fs chưa đo.** Harness có hỗ trợ format f2fs, nhưng đợt này chỉ chạy ext4/XFS/btrfs/ZFS. Không có số f2fs nào trong bài.
- **Bài này không đo workload metrics.** Phần jsonb/norm và `temp_sort` thuộc bài benchmark metrics trước, với 1.600 series và 288.000 sample mỗi backend — cùng harness, khác workload. Trộn hai bộ số sẽ tạo ra một bảng không so sánh được.
- Chiều cao btree (3 level ở 10M, 4 ở 100M) tính từ fan-out của page, không đo bằng `pageinspect`.
- Shape prefix và substring có `LIMIT 100` trên bảng sắp theo id, cho phép seq scan thoát sớm; đọc caveat ở mục 4.
- Thí nghiệm `parallel_workers` không kết luận được ở size và số CPU này.
- Read sweep (1/4/16/64/256 connections) chỉ chạy trên ext4.
- **Chỉ một instance.** Toàn bộ bài chạy trên một instance 4 vCPU. Sharding qua nhiều instance chưa đo, nên kết luận về write không áp dụng cho trường hợp đó: instance thứ hai mang theo fsync lane thứ hai.

Toàn bộ code, scenario và artifact nằm trong `benchctl/` tại
[github.com/hungpham10/my-benchmarks](https://github.com/hungpham10/my-benchmarks).
Số liệu tổng hợp và danh sách run ở
`benchctl/reports/2026-10-21-pg-index-shard-read.md` và
`benchctl/reports/2026-10-21-pg-user-model-fs.md`. Trước đó tôi dùng
cùng harness để đo [PostgreSQL làm metrics store](/blog/prometheus-vs-postgres-metrics-vi).
Bản tiếng Anh của bài này ở [bản tiếng Anh](/blog/postgres-sharding-filesystem-en).
