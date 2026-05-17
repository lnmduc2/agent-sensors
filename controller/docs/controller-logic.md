# Controller Logic

## 1. Mục tiêu

Controller là một **long-running server** được khởi động từ một `controller.yaml`, ví dụ:

```bash
sensor controller start
```

hoặc nếu muốn chỉ rõ file config:

```bash
sensor controller start --file ./controller.yaml
```

Sau khi chạy, Controller sẽ:

- quét thư mục sensors
- validate các sensor tìm thấy
- giữ runtime state trong memory
- expose HTTP server cho UI
- cho phép user thao tác lifecycle sensor qua dashboard/CLI của controller
- stream log realtime từ các sensor đang chạy
- làm owner duy nhất của mọi sensor processes do nó spawn ra

Controller là lớp vận hành của Sensor ở mức project-local, single-user, single-machine.

## 2. Phạm vi tối thiểu

Cho v1 dùng hằng ngày, Controller chỉ cần hỗ trợ:

- đọc `controller.yaml`
- scan sensors từ một danh sách `sensors_paths`
- validate manifest cơ bản
- start/stop/restart từng sensor
- list versions local/remote
- pull sensor từ registry
- expose dashboard UI trên một port local
- hiển thị trạng thái, log realtime, và vài thống kê nhẹ

Không cần cho v1:

- multi-user auth
- multi-machine orchestration
- HA/failover
- distributed locking
- sandbox bảo mật mạnh
- autoscaling

## 3. Entry point

Controller được xem là một loại runtime đặc biệt của CLI.

Command tối thiểu:

```bash
sensor controller start
```

hoặc:

```bash
sensor controller start --file <path-to-controller.yaml>
```

### 3.1. `controller.yaml`

`controller.yaml` là manifest cấu hình cho một controller instance.

Nó nên đóng vai trò tương tự `docker-compose.yml`: mô tả controller sẽ quản lí những sensor nào và server sẽ chạy với policy nào.

Shape tối thiểu gợi ý:

```yaml
api_version: controller/v0.1
name: local-dev
server:
  port: 4319
sensors_paths:
  - ./.sensors
  - ../shared-sensors
registry:
  index_url: https://...
```

### 3.2. Input tối thiểu

- file `controller.yaml` ở thư mục hiện tại hoặc được chỉ ra bởi `--file`
- `server.port`
- `sensors_paths[]`: danh sách thư mục có thể chứa sensor folders

### 3.3. Hành vi khi start controller

1. resolve file `controller.yaml`
2. parse config
3. validate config
4. kiểm tra từng path trong `sensors_paths` có tồn tại không
5. scan toàn bộ sensor folders từ tất cả paths đã cấu hình
6. validate từng sensor
7. build in-memory registry của các sensor discover được
8. start HTTP server
9. start background loops cần thiết:
   - rescan loop
   - process supervision loop
   - artifact freshness loop
10. publish dashboard UI

## 4. Mental model

Controller không phải là một sensor.

Nó là **project-local control plane** cho các sensor trong một workspace.

Ba lớp cần tách rõ:

- **Sensor Spec**: `sensor.yaml` + `runner.*`
- **Controller Server**: scan, validate, lifecycle, logs, state
- **Controller UI**: dashboard gọi vào controller server

UI không thao tác process trực tiếp. Mọi thao tác đều đi qua Controller server.
Agent cũng không nên có quyền gọi lifecycle APIs; agent chỉ nên observe sink, status, hoặc logs ở mức read-only nếu user chủ động expose.

### 4.1. Ownership rule

- chỉ Controller được phép spawn và stop sensor processes
- chỉ user được phép kích hoạt lifecycle actions qua dashboard hoặc CLI của controller
- agent không được quyền tự chạy `runner.py` hoặc gọi mutating lifecycle APIs
- nếu Controller thoát, mọi sensor processes do Controller spawn ra phải bị dọn sạch

Rule này tồn tại để tránh orphan processes khi agent hoặc một workflow trung gian chết giữa chừng.

## 5. Thực thể logic tối thiểu

### 5.1. Sensor Definition

Thông tin tĩnh lấy từ disk:

- `name`
- `version`
- `description`
- `sensor_path`
- `manifest`
- `runtime.entrypoint`
- `runtime.language`
- `source.type`
- `sink.type`
- `sink.target`
- `agent_hints.freshness_sla`

### 5.2. Sensor Runtime

Thông tin động do Controller quản lí:

- `state`
- `pid`
- `started_at`
- `stopped_at`
- `last_exit_code`
- `last_error`
- `restart_count`
- `last_log_at`
- `last_artifact_update_at`
- `artifact_health`

### 5.3. Controller State

Thông tin mức server:

- `started_at`
- `controller_file`
- `controller_name`
- `sensors_paths[]`
- `port`
- `sensor_count`
- `running_count`
- `idle_count`
- `error_count`
- `invalid_count`
- `stale_count`

## 6. State model

### 6.1. Sensor runtime states

- `discovered`: đã quét thấy nhưng chưa validate xong
- `invalid`: manifest hoặc runner lỗi
- `idle`: hợp lệ nhưng chưa chạy
- `starting`: đang spawn runner
- `running`: runner còn sống
- `stopping`: đang dừng runner
- `stopped`: vừa dừng sạch
- `error`: runtime lỗi hoặc exit bất thường
- `pulling`: đang pull version/package

### 6.2. Health overlay

Ngoài state chính, mỗi sensor có health phụ:

- `healthy`
- `stale`
- `unknown`

Ví dụ một sensor có thể là:

- state `running`
- health `stale`

## 7. Scan logic

Controller phải xem `controller.yaml` là source of truth cho discovery.

### 7.1. Discovery rule

Mỗi item trong `sensors_paths[]` là một root có thể chứa nhiều sensor folders.
Một thư mục con được xem là sensor candidate khi có `sensor.yaml`.

Ví dụ:

```yaml
sensors_paths:
  - ./.sensors
  - ../shared-sensors
  - C:/company/sensors
```

và mỗi root có thể có dạng:

```text
shared-sensors/
  pencil-stdout/
    sensor.yaml
    runner.py
  api-latency/
    sensor.yaml
    runner.js
```

### 7.2. Initial scan

Khi controller boot:

1. duyệt từng root trong `sensors_paths[]`
2. liệt kê tất cả thư mục con trong từng root
3. tìm `sensor.yaml`
4. parse manifest
5. validate cơ bản
6. đưa sensor vào danh sách quản lí

### 7.3. Naming and dedup rule

Vì sensor có thể đến từ nhiều roots khác nhau, controller cần rule resolve rõ ràng.

Tối thiểu nên có:

- `name` phải unique trong phạm vi một controller instance
- nếu hai sensor khác path nhưng trùng `name`, controller đánh dấu conflict và không tự chọn một cái
- conflict này phải hiện rõ trên dashboard để user sửa config hoặc đổi tên sensor

### 7.4. Rescan loop

Để đủ practical cho daily use, controller nên có rescan định kỳ nhẹ, ví dụ mỗi 2-5 giây.

Mục tiêu:

- phát hiện sensor mới được thêm vào ở bất kỳ root nào
- phát hiện sensor bị xóa
- phát hiện manifest thay đổi
- phát hiện conflict mới do trùng tên

### 7.5. Reconciliation rule

Khi rescan:

- sensor mới -> thêm vào dashboard với state `idle` hoặc `invalid`
- sensor sửa manifest -> revalidate
- sensor bị xóa nhưng đang không chạy -> remove khỏi dashboard
- sensor bị xóa khi đang chạy -> giữ runtime record tạm thời và đánh dấu warning cho tới khi process dừng
- sensor conflict do trùng `name` -> đánh dấu `invalid` hoặc `error_config` ở mức controller

## 8. Validate logic

Validate được gọi ở hai chỗ:

- lúc scan/rescan
- khi user bấm validate trên UI hoặc API

### 8.1. Các mức validate tối thiểu

#### Parse level

- có `sensor.yaml`
- YAML parse được

#### Schema level

- có `api_version`, `name`, `version`, `description`
- có `runtime.entrypoint`, `runtime.language`
- có `source.type`, `source.target`
- có `sink.type`, `sink.target`

#### Convention level

- folder name khớp `name`
- entrypoint tồn tại

#### Supportability level

- `runtime.language` là loại controller biết chạy
- `sink.type` là loại controller biết kiểm tra freshness ở mức cơ bản, nếu cần

### 8.2. Validate result

Kết quả trả về nên có:

- `valid: boolean`
- `errors[]`
- `warnings[]`
- `normalized_manifest`
- `validated_at`

Nếu invalid, sensor vẫn phải xuất hiện trên dashboard để user sửa.

## 9. Lifecycle logic

Mọi lifecycle action đều đi qua controller server.
Controller là lifecycle owner duy nhất của sensor processes trong phiên chạy đó.

## 9.1. Start sensor

Flow:

1. lấy sensor theo `name`
2. nếu invalid thì từ chối start
3. nếu đang `running` hoặc `starting` thì no-op hoặc trả conflict
4. set state `starting`
5. spawn runner process theo `runtime.language`
6. đăng ký process đó vào process group / ownership scope của controller nếu OS hỗ trợ
7. attach `stdout`/`stderr`
8. set state `running`
9. lưu `pid`, `started_at`
10. bắt đầu stream log vào log buffer/log file

## 9.2. Stop sensor

Flow:

1. lấy runtime theo `name`
2. nếu không chạy thì no-op
3. set state `stopping`
4. gửi terminate signal phù hợp OS
5. chờ timeout ngắn
6. nếu chưa dừng thì force kill
7. cập nhật `stopped_at`, `last_exit_code`, state `stopped` hoặc `idle`

## 9.3. Restart sensor

Flow:

1. stop
2. start lại bằng definition hiện tại trên disk
3. tăng `restart_count`

## 9.4. Start-all / stop-all

Không bắt buộc cho v1, nhưng nếu thêm thì chỉ là lặp qua từng sensor với cùng lifecycle API.

## 10. Process execution model

Controller phải tự biết cách chạy runner theo `runtime.language`.

Ví dụ mapping tối thiểu:

- `python` -> `python <entrypoint>`
- `node` -> `node <entrypoint>`
- `bash` -> shell tương ứng
- `binary` -> execute trực tiếp

Controller không cần hiểu business logic của runner; chỉ cần spawn, stop, monitor, capture logs.
Cho v1, không cần watcher process riêng cho từng sensor. Việc giám sát nên là supervision task trong chính controller process.

## 11. Logging model

Có hai lớp dữ liệu khác nhau.

### 11.1. Runner log

Là `stdout` + `stderr` từ process runner.

Controller phải:

- capture từng dòng
- đóng timestamp
- gắn sensor name và stream type
- lưu vào memory buffer gần đây
- tùy chọn ghi ra file local để debug sau

### 11.2. Controller event log

Là log nội bộ về các action như:

- `DISCOVERED`
- `VALIDATED`
- `START_REQUESTED`
- `STARTED`
- `STOPPED`
- `EXITED`
- `PULL_STARTED`
- `PULL_FINISHED`
- `VALIDATION_FAILED`

UI nên thấy cả runner log lẫn controller event log trong cùng log stream của sensor.

## 12. Log streaming

Vì UI là web UI, controller phải hỗ trợ realtime push.

Cách tối thiểu:

- WebSocket
- hoặc Server-Sent Events

Mỗi khi sensor có log mới:

- append vào ring buffer trong memory
- push event tới client nào đang subscribe log của sensor đó

Để v1 đơn giản, mỗi sensor chỉ cần giữ `N` dòng gần nhất trong memory.

## 13. Artifact freshness logic

Controller không đọc nội dung artifact thay agent, nhưng nên kiểm tra độ tươi tối thiểu để dashboard có health signal.

### 13.1. Với sink file-based

Nếu sink là:

- `file.append`
- `file.ring`
- `file.snapshot`

thì controller kiểm tra:

- target path có tồn tại không
- `mtime` gần nhất là khi nào
- nếu có `agent_hints.freshness_sla` thì age có vượt SLA không

### 13.2. Kết quả

Controller cập nhật:

- `last_artifact_update_at`
- `artifact_health`
- `artifact_age_ms`

Nếu process đang chạy nhưng artifact stale, UI nên hiện warning chứ không tự coi là crashed.

## 14. Registry và version logic

Controller vẫn có package concerns tối thiểu.

## 14.1. List versions

Nguồn version:

- local active version trong các roots của `sensors_paths[]`
- local pulled cache
- remote registry index

## 14.2. Pull

Flow tối thiểu:

1. nhận `name` + `version` hoặc latest
2. đọc registry index
3. resolve package
4. tải package về local cache
5. validate package sau pull
6. báo kết quả cho UI

Cho v1, `pull` không bắt buộc tự động activate vào `sensors-path`.

Có thể tách hai bước:

- `pull`
- `install` hoặc thay file bằng tay

Nếu muốn gọn hơn cho daily use, có thể cho `pull and install` nhưng phải xác nhận rõ nếu sensor đang chạy.

## 14.3. Vì sao dùng `controller.yaml`

Model này hợp lí vì:

- controller trở thành một unit config rõ ràng có thể commit vào repo
- UX gần với `docker compose`, dễ hiểu và dễ nhớ
- một controller instance có thể gom sensor từ nhiều nơi khác nhau
- policy của controller không làm bẩn `sensor.yaml`

`controller.yaml` mô tả orchestration scope; `sensor.yaml` vẫn chỉ mô tả một sensor riêng lẻ.

## 15. HTTP API tối thiểu

UI cần một API nhỏ nhưng rõ ràng.

### 15.1. Read APIs

- `GET /api/controller`
- `GET /api/sensors`
- `GET /api/sensors/:name`
- `GET /api/sensors/:name/logs`
- `GET /api/sensors/:name/versions`

### 15.2. Action APIs

- `POST /api/sensors/:name/validate`
- `POST /api/sensors/:name/start`
- `POST /api/sensors/:name/stop`
- `POST /api/sensors/:name/restart`
- `POST /api/sensors/:name/pull`
- `POST /api/rescan`

### 15.3. Streaming APIs

- `GET /api/stream/sensors`
- `GET /api/stream/sensors/:name/logs`

Mục đích:

- stream sensor state changes
- stream log realtime

## 16. Persistence tối thiểu

Cho v1, source of truth chính là:

- sensors trên disk
- processes đang chạy
- state in-memory của controller

Persistence tối thiểu chỉ cần cho debug/recovery nhẹ:

```text
.controller/
  logs/
  cache/
```

Có thể lưu:

- pulled packages
- registry cache
- log files

Không nhất thiết phải persist toàn bộ runtime state nếu controller restart.

Khi controller restart, nó chỉ cần rescan lại sensors và coi tất cả là `idle`, trừ khi có cơ chế attach lại process cũ.

Để v1 đơn giản, **không cần attach lại process cũ**.
Controller cũng không cố giữ sensor sống độc lập với chính nó; nếu controller shutdown bình thường hoặc bị `Ctrl+C`, nó phải cố stop toàn bộ sensor processes trước khi thoát.

## 16.1. Shutdown semantics

Khi controller nhận shutdown signal như `Ctrl+C`:

1. ngừng nhận lifecycle requests mới
2. đánh dấu UI/server là đang shutdown
3. gửi stop tới toàn bộ sensor đang `running` hoặc `starting`
4. chờ một grace timeout ngắn
5. force-kill phần còn lại nếu cần
6. flush log cần thiết rồi mới thoát process controller

Mục tiêu là không để lại orphan sensor processes sau khi user thôi dùng controller.

## 17. Error model tối thiểu

Nên chuẩn hóa lỗi theo nhóm:

- `scan_error`
- `validation_error`
- `runtime_error`
- `registry_error`
- `stream_error`

Mỗi lỗi nên có:

- `code`
- `message`
- `sensor_name` nếu có
- `time`

## 18. Mức đủ dùng của controller v1

Controller được coi là đạt mục tiêu nếu sau khi chạy:

```bash
sensor controller start
```

người dùng có thể mở UI và làm được các việc sau:

- thấy tất cả sensor mà controller quét được
- biết sensor nào `running`, `idle`, `error`, `invalid`
- xem log realtime của từng sensor
- start/stop/restart sensor ngay trên dashboard
- validate sensor khi manifest hoặc runner đổi
- pull version mới từ registry khi cần
- thấy artifact có stale hay không

Nếu đạt được các hành vi trên thì model controller server + web UI đã hợp với use case project cá nhân hằng ngày.
