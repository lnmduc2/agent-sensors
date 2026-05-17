# Controller UI

## 1. Mục tiêu

Controller UI là **web dashboard** được serve bởi Controller server sau khi chạy:

```bash
sensor controller start
```

Người dùng mở browser vào local UI và thấy:

- controller đang quét được những sensor nào
- sensor nào đang `running` / `idle` / `error` / `invalid`
- log realtime của từng sensor
- một vài thống kê tổng quan
- các nút thao tác lifecycle ngay trên giao diện

UI này không tự quản lí process. Nó chỉ hiển thị state và gửi action tới Controller server.
UI này cũng là bề mặt control dành cho user; agent không được xem là actor có quyền lifecycle trên sensor.

## 2. Mental model

UI là dashboard cho **một controller instance**.

Một controller instance tương ứng với:

- một `controller.yaml`
- một HTTP server local
- một danh sách `sensors_paths[]`
- một tập sensor được quét từ toàn bộ các paths đó

Người dùng vào UI là đang nhìn trạng thái hiện tại của controller đó.
UI này là nơi user quản lí lifecycle; agent không được coi là actor có quyền start/stop/restart sensor từ đây.

## 3. Màn hình chính

Dashboard v1 chỉ cần một màn hình chính với 4 vùng:

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Header: controller info + summary stats + rescan/refresh            │
├──────────────────────────────────────────────────────────────────────┤
│ Sensor table / cards                                                 │
│ - mỗi row là một sensor được controller quét thấy                    │
│ - có state, health, version, artifact age, action buttons            │
├──────────────────────────────────────────────────────────────────────┤
│ Detail panel của sensor đang chọn                                    │
│ - manifest/runtime summary                                            │
│ - errors/warnings                                                     │
│ - versions                                                            │
├──────────────────────────────────────────────────────────────────────┤
│ Realtime log panel                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

Nếu muốn tối giản hơn nữa, detail panel và log panel có thể nằm cùng một cột bên phải.

## 4. Header behavior

Header phải luôn hiển thị context của controller:

- `controller name`
- `controller file`
- `sensors paths`
- `port`
- `controller started at`
- trạng thái kết nối UI -> server

Và các thống kê tổng quan:

- `total`
- `running`
- `idle`
- `error`
- `invalid`
- `stale`

Ngoài ra có các action mức controller:

- `Rescan`
- `Refresh`

Nếu có background action đang chạy thì header nên hiện badge như:

- `rescanning`
- `pulling`
- `validating`

## 5. Sensor list behavior

## 5.1. Nguồn dữ liệu

Sensor list luôn được build từ danh sách do Controller server trả về, không phải UI tự scan disk.

## 5.2. Mỗi row cần có

- `name`
- `version`
- `state`
- `health`
- `source root` hoặc `sensor path`
- `source type`
- `sink type`
- `artifact age` hoặc `freshness`
- `pid` nếu có
- `last error` rút gọn nếu có
- action buttons

Ví dụ row:

```text
pencil-stdout | 1.0.0 | running | healthy | process.stdout -> file.ring | 0.6s | pid 2213
api-latency   | 0.3.1 | error   | unknown | http.poll -> file.snapshot  | -    | spawn failed
```

## 5.3. Sorting mặc định

Thứ tự mặc định nên ưu tiên để người dùng thấy chỗ hỏng trước:

1. `error`
2. `invalid`
3. `stale`
4. `running`
5. `idle`

## 5.4. Filtering tối thiểu

Nên có filter nhanh theo:

- all
- running
- idle
- error
- invalid
- stale

Không cần search phức tạp ở v1, nhưng filter state là rất nên có.

## 6. Action buttons trên từng sensor

Các action này là quyền điều khiển của user trên dashboard.
Controller nhận request, thực thi lifecycle, rồi phát state mới về UI.

Mỗi sensor row hoặc detail panel nên có các action tối thiểu:

- `Validate`
- `Start`
- `Stop`
- `Restart`
- `Pull`

Rule:

- `Start` enable khi sensor đang `idle` hoặc `error` sau khi đã fix
- `Stop` enable khi sensor đang `running` hoặc `starting`
- `Restart` enable khi sensor đang `running` hoặc `error`
- `Validate` luôn có
- `Pull` luôn có nếu registry được cấu hình

Khi click action:

1. UI gửi request tới API
2. button chuyển sang loading state ngắn
3. khi server update state, row và detail tự refresh

UI không nên optimistic update quá nhiều; nên tin server state.

## 7. Detail panel behavior

Khi user chọn một sensor, detail panel phải hiện tối thiểu:

- `name`
- `version`
- `description`
- `sensor path`
- `runtime.entrypoint`
- `runtime.language`
- `source.type`
- `sink.type`
- `sink.target`
- `state`
- `health`
- `pid`
- `started_at`
- `stopped_at`
- `last_exit_code`
- `last_error`
- `last_artifact_update_at`
- `freshness_sla`

Nếu sensor invalid, detail panel phải ưu tiên hiện `validation errors` và `warnings` thay vì chỉ metadata đẹp.

## 8. Realtime log panel

Đây là phần quan trọng nhất sau sensor table.

### 8.1. Hành vi chính

- khi user chọn sensor, log panel subscribe vào log stream của sensor đó
- log mới phải xuất hiện gần realtime
- hiển thị cả runner logs và controller events
- tự scroll xuống cuối khi user đang ở cuối
- nếu user scroll lên xem log cũ thì tạm dừng auto-scroll
- có nút `Jump to latest`

### 8.2. Dữ liệu mỗi dòng log

Mỗi dòng nên có tối thiểu:

- timestamp
- stream/type (`stdout`, `stderr`, `controller`)
- message

Ví dụ:

```text
14:20:01 [controller] START_REQUESTED
14:20:01 [controller] STARTED pid=2213
14:20:02 [stdout] connected to target process
14:20:04 [stderr] transient warning: retrying
```

### 8.3. Empty states

- chưa có log -> hiện `No logs yet`
- sensor invalid -> có thể hiện validation output thay log
- sensor chưa từng chạy -> hiện message rõ ràng thay vì panel trống

## 9. Realtime state updates

UI phải phản ứng với state changes từ server mà không cần reload page.

Nguồn update có thể là:

- WebSocket
- hoặc SSE

Các loại event tối thiểu:

- sensor discovered
- sensor removed
- validation updated
- state changed
- process exited
- freshness updated
- log appended

Behavior mong muốn:

- sensor table cập nhật ngay khi state đổi
- counters trên header cập nhật theo
- detail panel của sensor đang chọn cập nhật theo
- log panel append dòng mới liên tục

## 10. Pull flow trong UI

Vì user có nhu cầu `list versions` và `pull`, UI cần flow tối thiểu sau:

1. user bấm `Pull` trên sensor
2. UI mở panel/modal nhỏ
3. UI tải version list local/remote từ server
4. user chọn version hoặc `latest`
5. user xác nhận pull
6. UI hiện trạng thái `pulling`
7. khi xong, UI hiện kết quả:
   - pull thành công
   - package được cache ở đâu
   - có cần install/restart hay không

Nếu muốn giữ v1 rất gọn, có thể cho `Pull latest` trực tiếp và chỉ hiện toast kết quả.

## 11. Rescan behavior

Vì controller scan từ `controller.yaml` và `sensors_paths[]`, UI phải có action `Rescan`.

Khi user bấm `Rescan`:

1. UI gọi API rescan
2. header hiện trạng thái `rescanning`
3. khi server xong, sensor list cập nhật:
   - sensor mới xuất hiện
   - sensor đổi manifest được revalidate
   - sensor bị xóa được remove hoặc đánh warning

## 12. Hiển thị state và health

UI nên tách rõ `state` và `health`.

### 12.1. State text

- `invalid`
- `idle`
- `starting`
- `running`
- `stopping`
- `stopped`
- `error`
- `pulling`

### 12.2. Health text

- `healthy`
- `stale`
- `unknown`

Ví dụ hiển thị:

- `running / healthy`
- `running / stale`
- `error / unknown`

Điều này quan trọng vì một sensor có thể chạy nhưng artifact không còn fresh.

## 12.3. Read-only vs control boundary

UI có thể hiển thị sink/status/logs cho mục đích quan sát, nhưng các action mutate lifecycle là quyền control của user.
Nếu sau này controller expose dữ liệu cho agent, nên tách rõ:

- endpoints read-only cho observe
- endpoints control chỉ dành cho user session local

## 13. Failure states cần hiển thị rõ

### 13.1. Không có sensor nào

Nếu controller quét không thấy sensor nào, UI phải hiện rõ:

- controller file hiện tại là gì
- các paths đang quét là gì
- không tìm thấy sensor candidates
- gợi ý thêm thư mục có `sensor.yaml` vào `controller.yaml`

### 13.2. Controller mất kết nối

Nếu browser mất kết nối với server:

- header phải hiện `disconnected`
- action buttons disable tạm thời
- UI cố reconnect nền

### 13.3. Sensor invalid

- row vẫn hiện trong list
- không cho start
- detail panel hiện validation errors cụ thể

### 13.4. Sensor name conflict

Vì controller có thể quét nhiều roots, UI phải hiện rõ khi có hai sensor trùng `name`.

- các row conflict vẫn hiện trong list
- không cho start các sensor bị conflict
- detail panel phải hiện cả hai paths đang va chạm
- user được hướng dẫn sửa `controller.yaml` hoặc đổi tên sensor

### 13.5. Sensor process chết bất thường

- row chuyển sang `error`
- detail panel hiện `last_exit_code` và `last_error`
- log panel giữ lại log cuối cùng để debug

## 14. Mức đủ dùng của UI v1

UI được coi là đúng mục tiêu nếu sau khi chạy controller server, user mở dashboard và có thể:

- thấy các sensor được controller quét từ `controller.yaml` và `sensors_paths[]`
- thấy state hiện tại của từng sensor
- start/stop/restart từng sensor ngay trên UI
- xem log realtime của sensor đang chọn
- validate sensor
- pull version mới
- rescan khi thư mục sensors thay đổi
- biết sensor nào đang stale hoặc invalid

Nếu làm được các hành vi trên thì UI đã khớp đúng model `controller server + browser dashboard` cho use case của project này.
