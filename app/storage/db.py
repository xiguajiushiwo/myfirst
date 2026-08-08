"""MySQL 持久化：质检记录 + 操作人。

- 配置从 .env 读取（MYSQL_HOST/PORT/USER/PASSWORD/DB）。
- 首次启动自动建库建表；操作人可增删、质检记录可存/查。
- 每条质检记录绑定序列号(SN) + 操作人，便于日后追溯。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import secrets

import pymysql

from .. import metrics

log = logging.getLogger("yxq.db")


def _load_dotenv():
    """读取项目根 .env（幂等，不覆盖已存在的真实环境变量）。

    从本文件所在目录**逐级向上**查找 .env（与模块在包内的层级无关，挪目录也不会坏）。
    """
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        path = os.path.join(d, ".env")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    # 行内注释：值未被引号包裹时，剥掉 ` #`（空白+井号）之后的内容。
                    # 例：`HIK_RESEND=1          # GigE 丢包自动重传` → 值取 "1"，不再连注释一起吃进去
                    #（导致 int("1  # ...") 抛错、相机在设重传参数时崩掉、预览永远无画面）。
                    # 引号包裹的值(如可能含 # 的密码)整体保留，只去掉外层引号。
                    if v[:1] in ('"', "'"):
                        q = v[0]
                        end = v.find(q, 1)
                        v = v[1:end] if end > 0 else v[1:]
                    else:
                        for i, ch in enumerate(v):
                            if ch == "#" and (i == 0 or v[i - 1] in " \t"):
                                v = v[:i]
                                break
                        v = v.strip()
                    if k:
                        os.environ.setdefault(k, v)
            return
        d = os.path.dirname(d)


_load_dotenv()

_CFG = {
    "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "user": os.environ.get("MYSQL_USER", "root"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "charset": "utf8mb4",
    "autocommit": True,
}
_DB = os.environ.get("MYSQL_DB", "yunxiaoquan_qc")


def _connect(with_db: bool = True):
    cfg = dict(_CFG)
    if with_db:
        cfg["database"] = _DB
    return pymysql.connect(**cfg)


# inspection_records 除 id/created_at 外的全部列（含 Phase 2 追溯图片列）。
# 用于**增量迁移**：缺哪列就 ALTER 补哪列，绝不 DROP，改表不丢历史数据。
_RECORD_COLUMNS = {
    "operator": "VARCHAR(64) DEFAULT ''",
    "sn": "VARCHAR(128) DEFAULT ''",
    "brand": "VARCHAR(64) DEFAULT ''",
    "model": "VARCHAR(128) DEFAULT ''",
    "frequency": "VARCHAR(32) DEFAULT ''",
    "spec": "VARCHAR(128) DEFAULT ''",        # 二维码(L)完整规格，如 64GB 2Rx4 PC5-5600B-RA0-1010-XT
    "mfg": "VARCHAR(64) DEFAULT ''",          # 二维码(M)厂商/批次码，如 TT9P000
    "controller_date": "CHAR(6) DEFAULT NULL",
    "pcb_date": "CHAR(6) DEFAULT NULL",
    "storage_chips": "JSON",
    "storage_count": "INT DEFAULT 0",
    "comp_ok": "TINYINT DEFAULT NULL",
    "gold_finger_ok": "TINYINT DEFAULT NULL",
    "chip_mark_ok": "TINYINT DEFAULT NULL",
    "date_ok": "TINYINT DEFAULT NULL",
    "verdict": "VARCHAR(16) DEFAULT ''",
    "fail_desc": "VARCHAR(255) DEFAULT ''",
    "review_status": "VARCHAR(16) DEFAULT '未复查'",
    # Phase 2：追溯图片（原图 + 标注图的归档相对路径）
    "front_img": "VARCHAR(255) DEFAULT ''",
    "back_img": "VARCHAR(255) DEFAULT ''",
    "annotated_front": "VARCHAR(255) DEFAULT ''",
    "annotated_back": "VARCHAR(255) DEFAULT ''",
    # Phase 4：批次登记（客户/容量/批次号；品牌·频率沿用 brand/frequency 列）
    "customer": "VARCHAR(128) DEFAULT ''",
    "supplier": "VARCHAR(128) DEFAULT ''",   # 供应商（随订单归集）
    "capacity": "VARCHAR(32) DEFAULT ''",
    "batch_no": "VARCHAR(64) DEFAULT ''",
    "cond": "VARCHAR(16) DEFAULT ''",        # 品相：拆机/拆新/全新
    "remark": "VARCHAR(255) DEFAULT ''",     # 备注（主要填来源，如 香港货/马来货）
    # 托盘拆分：一盘 N 根拆成 N 条记录，slot_pos = 托盘第几槽(1..N，左→右)；单根/规则模式为 NULL
    "slot_pos": "INT DEFAULT NULL",
    # 单次手动检测追踪：同一盘四根共享 inspection_id / timing / token_usage
    "inspection_id": "VARCHAR(32) DEFAULT ''",
    "recognition_mode": "VARCHAR(16) DEFAULT 'rules'",
    "timing": "JSON",
    "elapsed_sec": "DECIMAL(10,3) DEFAULT 0",
    "token_usage": "JSON",
    "label_data": "JSON",
}

# users 表除 id/username/created_at 外的列（增量迁移用）
_USER_COLUMNS = {
    "pw_hash": "VARCHAR(128) NOT NULL",
    "salt": "VARCHAR(64) NOT NULL",
    "role": "VARCHAR(16) DEFAULT 'operator'",       # admin / operator
    "status": "VARCHAR(16) DEFAULT 'pending'",      # pending / approved / rejected
    "phone": "VARCHAR(32) DEFAULT ''",              # 手机号（注册填，管理员审核时可见/联系用）
    "reviewed_by": "VARCHAR(64) DEFAULT ''",
    "reviewed_at": "DATETIME DEFAULT NULL",
}

# batches 表除 id/created_at/active 外的列（增量迁移用）
# 语义 = 采购订单（前端显示"采购订单"，底层仍叫 batch，不改名）
_BATCH_COLUMNS = {
    "batch_no": "VARCHAR(64) DEFAULT ''",
    "customer": "VARCHAR(128) DEFAULT ''",
    "brand": "VARCHAR(64) DEFAULT ''",
    "capacity": "VARCHAR(32) DEFAULT ''",
    "frequency": "VARCHAR(32) DEFAULT ''",
    "cond": "VARCHAR(16) DEFAULT ''",        # 品相：拆机/拆新/全新
    "remark": "VARCHAR(255) DEFAULT ''",     # 备注/来源
    # --- 采购订单扩展字段 ---
    "model": "VARCHAR(128) DEFAULT ''",          # 型号
    "supplier": "VARCHAR(128) DEFAULT ''",       # 供应商
    "qty_expected": "INT DEFAULT 0",             # 应检数量（0=未填/不限）
    "delivery_date": "DATE DEFAULT NULL",        # 交期
    # --- OA 对接预留（真 API 到位再填充）---
    "oa_order_no": "VARCHAR(64) DEFAULT ''",     # OA 采购订单号（手工录入可空）
    "oa_synced": "TINYINT DEFAULT 0",            # 1=由 OA 同步而来
    "oa_raw": "JSON",                            # OA 原始返回（预留追溯）
    # --- 金蝶采购订单扩展字段（只读同步）---
    "kd_bill_status": "VARCHAR(16) DEFAULT ''",      # 单据状态
    "kd_close_status": "VARCHAR(16) DEFAULT ''",     # 关闭状态
    "kd_pay_mode": "VARCHAR(32) DEFAULT ''",         # 付款方式
    "kd_operator": "VARCHAR(64) DEFAULT ''",         # 业务员
    "kd_supplier_number": "VARCHAR(64) DEFAULT ''",  # 供应商编码
    "kd_supplier_status": "VARCHAR(32) DEFAULT ''",  # 供应商状态
    "kd_material_number": "VARCHAR(64) DEFAULT ''",  # 物料编码
    "kd_src_bill_no": "VARCHAR(64) DEFAULT ''",      # 来源销售单
    "kd_specification": "VARCHAR(128) DEFAULT ''",   # 金蝶规格型号
    "kd_total_amount": "DECIMAL(18,2) DEFAULT 0",    # 未税金额
    "kd_total_all_amount": "DECIMAL(18,2) DEFAULT 0",# 价税合计
    "kd_tax_amount": "DECIMAL(18,2) DEFAULT 0",      # 税额
    "kd_received_qty": "DECIMAL(18,2) DEFAULT 0",    # 已收货数量
    "kd_in_stock_qty": "DECIMAL(18,2) DEFAULT 0",    # 已入库数量
    "kd_return_qty": "DECIMAL(18,2) DEFAULT 0",      # 已退货数量
}


def _ensure_columns(cur, table: str, columns: dict) -> None:
    """增量迁移：表已存在时，缺哪列补哪列（ALTER ADD），**不 DROP、不动数据**。"""
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s", (_DB, table))
    have = {r[0].lower() for r in cur.fetchall()}
    for col, ddl in columns.items():
        if col.lower() not in have:
            cur.execute(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}")
            log.info("迁移：%s 增列 %s", table, col)


def init_db():
    """建库 + 建表（幂等）+ 增量迁移。启动时调用一次。"""
    conn = _connect(with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{_DB}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            cur.execute(f"USE `{_DB}`")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS operators (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(64) NOT NULL UNIQUE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            # 记录表：全新库用完整建表；已存在的老库靠 _ensure_columns 补列（不 DROP）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS inspection_records (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    operator VARCHAR(64) DEFAULT '',
                    sn VARCHAR(128) DEFAULT '',
                    brand VARCHAR(64) DEFAULT '',
                    model VARCHAR(128) DEFAULT '',
                    frequency VARCHAR(32) DEFAULT '',
                    controller_date CHAR(6) DEFAULT NULL,
                    pcb_date CHAR(6) DEFAULT NULL,
                    storage_chips JSON,
                    storage_count INT DEFAULT 0,
                    comp_ok TINYINT DEFAULT NULL,
                    gold_finger_ok TINYINT DEFAULT NULL,
                    chip_mark_ok TINYINT DEFAULT NULL,
                    date_ok TINYINT DEFAULT NULL,
                    verdict VARCHAR(16) DEFAULT '',
                    fail_desc VARCHAR(255) DEFAULT '',
                    review_status VARCHAR(16) DEFAULT '未复查',
                    front_img VARCHAR(255) DEFAULT '',
                    back_img VARCHAR(255) DEFAULT '',
                    annotated_front VARCHAR(255) DEFAULT '',
                    annotated_back VARCHAR(255) DEFAULT '',
                    INDEX idx_sn (sn),
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            _ensure_columns(cur, "inspection_records", _RECORD_COLUMNS)
            # 批次登记：每批测试前登记 客户/品牌/容量/频率/批次号；每根质检归入当前批
            cur.execute("""
                CREATE TABLE IF NOT EXISTS batches (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    batch_no VARCHAR(64) DEFAULT '',
                    customer VARCHAR(128) DEFAULT '',
                    brand VARCHAR(64) DEFAULT '',
                    capacity VARCHAR(32) DEFAULT '',
                    frequency VARCHAR(32) DEFAULT '',
                    cond VARCHAR(16) DEFAULT '',
                    remark VARCHAR(255) DEFAULT '',
                    active TINYINT DEFAULT 1,
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            _ensure_columns(cur, "batches", _BATCH_COLUMNS)
            # 审计日志：谁在何时改了复查/删了模板·操作人 等
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    operator VARCHAR(64) DEFAULT '',
                    action VARCHAR(32) DEFAULT '',
                    target VARCHAR(128) DEFAULT '',
                    detail VARCHAR(255) DEFAULT '',
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            # 预置几个操作人，方便前台直接选
            cur.execute("SELECT COUNT(*) FROM operators")
            if cur.fetchone()[0] == 0:
                cur.executemany("INSERT INTO operators(name) VALUES(%s)",
                                [("质检员A",), ("质检员B",), ("管理员",)])
            # 登录账号：注册→管理员审核→通过后才能登录。角色 admin/operator，状态 pending/approved/rejected
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    pw_hash VARCHAR(128) NOT NULL,
                    salt VARCHAR(64) NOT NULL,
                    role VARCHAR(16) DEFAULT 'operator',
                    status VARCHAR(16) DEFAULT 'pending',
                    phone VARCHAR(32) DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    reviewed_by VARCHAR(64) DEFAULT '',
                    reviewed_at DATETIME DEFAULT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            _ensure_columns(cur, "users", _USER_COLUMNS)
            # 会话：登录发 token 存这里，浏览器持 HttpOnly cookie。过期即失效。
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token VARCHAR(64) PRIMARY KEY,
                    username VARCHAR(64) NOT NULL,
                    role VARCHAR(16) DEFAULT 'operator',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NOT NULL,
                    INDEX idx_exp (expires_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            # 种子超管：库里还没有任何 admin 时，按 .env 的 ADMIN_USER/ADMIN_PASSWORD 建一个
            cur.execute("SELECT COUNT(*) FROM users WHERE role='admin'")
            if cur.fetchone()[0] == 0:
                au = (os.environ.get("ADMIN_USER", "admin") or "admin").strip()
                ap = os.environ.get("ADMIN_PASSWORD", "admin123") or "admin123"
                salt = _make_salt()
                cur.execute(
                    "INSERT INTO users(username, pw_hash, salt, role, status) "
                    "VALUES(%s,%s,%s,'admin','approved') "
                    "ON DUPLICATE KEY UPDATE role='admin', status='approved'",
                    (au, _hash_pw(ap, salt), salt))
                log.info("已创建种子超管账号：%s（请首次登录后尽快改密）", au)
        log.info("MySQL 初始化/迁移完成")
    finally:
        conn.close()


def add_audit(operator: str, action: str, target: str, detail: str = "") -> None:
    """写一条审计日志（改复查/删除等敏感操作）。失败只告警不阻断。"""
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO audit_log(operator, action, target, detail) VALUES(%s,%s,%s,%s)",
                        ((operator or "")[:64], action[:32], str(target)[:128], (detail or "")[:255]))
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("写审计失败 %s/%s: %s", action, target, e)


def list_audit(limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id, created_at, operator, action, target, detail "
                        "FROM audit_log ORDER BY id DESC LIMIT %s", (int(limit),))
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
    return rows


# ----------------------------- 操作人 -----------------------------

def list_operators() -> list[str]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM operators ORDER BY created_at, id")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def add_operator(name: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO operators(name) VALUES(%s)", (name,))
        return True
    finally:
        conn.close()


def delete_operator(name: str) -> bool:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM operators WHERE name=%s", (name,))
            return cur.rowcount > 0
    finally:
        conn.close()


# ----------------------------- 登录账号 / 会话 -----------------------------
# 密码只存 pbkdf2 哈希 + 随机盐，绝不存明文（标准库 hashlib，零第三方依赖）。

def _make_salt() -> str:
    return secrets.token_hex(16)


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                               salt.encode("utf-8"), 200000).hex()


def create_user(username: str, password: str, role: str = "operator",
                status: str = "pending", phone: str = "") -> tuple[bool, str]:
    """注册新账号。用户名重复返回 (False, 原因)。默认 pending 待审核。"""
    username = (username or "").strip()
    if not username or not password:
        return False, "用户名和密码不能为空"
    if len(username) > 64:
        return False, "用户名过长"
    salt = _make_salt()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE username=%s", (username,))
            if cur.fetchone():
                return False, "用户名已存在"
            cur.execute("INSERT INTO users(username, pw_hash, salt, role, status, phone) "
                        "VALUES(%s,%s,%s,%s,%s,%s)",
                        (username, _hash_pw(password, salt), salt, role, status,
                         (phone or "").strip()[:32]))
        return True, "ok"
    finally:
        conn.close()


def get_user(username: str) -> dict | None:
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id, username, pw_hash, salt, role, status "
                        "FROM users WHERE username=%s", ((username or "").strip(),))
            return cur.fetchone()
    finally:
        conn.close()


def verify_login(username: str, password: str) -> dict | None:
    """校验用户名+密码。对上返回用户 dict（含 status/role），否则 None。不看审核状态。"""
    u = get_user(username)
    if not u:
        return None
    if _hash_pw(password, u["salt"]) != u["pw_hash"]:
        return None
    return u


def list_users(status: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            sql = ("SELECT id, username, role, status, phone, created_at, reviewed_by, reviewed_at "
                   "FROM users")
            args = []
            if status:
                sql += " WHERE status=%s"
                args.append(status)
            sql += " ORDER BY (status='pending') DESC, id DESC"
            cur.execute(sql, args)
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        for k in ("created_at", "reviewed_at"):
            if r.get(k) is not None:
                r[k] = r[k].strftime("%Y-%m-%d %H:%M:%S")
    return rows


def set_user_status(uid: int, status: str, by: str = "") -> bool:
    """审核：approved / rejected（也可 pending 撤回）。"""
    if status not in ("approved", "rejected", "pending"):
        return False
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET status=%s, reviewed_by=%s, reviewed_at=NOW() "
                        "WHERE id=%s", (status, (by or "")[:64], int(uid)))
            ok = cur.rowcount >= 0
        # 拒绝/撤回后，删掉该用户已有会话（立即失效）
        if status != "approved":
            with conn.cursor() as cur:
                cur.execute("DELETE s FROM sessions s JOIN users u ON s.username=u.username "
                            "WHERE u.id=%s", (int(uid),))
        return ok
    finally:
        conn.close()


def set_user_password(uid: int, password: str) -> bool:
    if not password:
        return False
    salt = _make_salt()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET pw_hash=%s, salt=%s WHERE id=%s",
                        (_hash_pw(password, salt), salt, int(uid)))
            return cur.rowcount > 0
    finally:
        conn.close()


def create_session(username: str, role: str, ttl_hours: int = 12) -> str:
    """登录成功后建会话，返回 token（存进 HttpOnly cookie）。"""
    token = secrets.token_urlsafe(32)
    exp = _dt.datetime.now() + _dt.timedelta(hours=int(ttl_hours))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sessions(token, username, role, expires_at) "
                        "VALUES(%s,%s,%s,%s)", (token, username, role, exp))
        return token
    finally:
        conn.close()


def get_session(token: str) -> dict | None:
    """token → {username, role}；不存在或已过期返回 None。"""
    if not token:
        return None
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT username, role, expires_at FROM sessions WHERE token=%s",
                        (token,))
            r = cur.fetchone()
        if not r:
            return None
        if r["expires_at"] and r["expires_at"] < _dt.datetime.now():
            delete_session(token)
            return None
        return {"username": r["username"], "role": r["role"]}
    finally:
        conn.close()


def delete_session(token: str) -> None:
    if not token:
        return
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
    finally:
        conn.close()


def purge_expired_sessions() -> None:
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE expires_at < NOW()")
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.warning("清理过期会话失败: %s", e)


# ----------------------------- 质检记录 -----------------------------

def _tri(v):
    """三态 bool：None 保留为 NULL（未检），否则 0/1。"""
    return None if v is None else (1 if v else 0)


def save_record(rec: dict) -> int:
    """保存一条质检记录，返回自增 id。"""
    chips = rec.get("storage_chips") or []
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO inspection_records
                    (operator, sn, brand, model, frequency, spec, mfg,
                     controller_date, pcb_date, storage_chips, storage_count,
                     comp_ok, gold_finger_ok, chip_mark_ok, date_ok,
                     verdict, fail_desc, review_status,
                     front_img, back_img, annotated_front, annotated_back,
                     customer, supplier, capacity, batch_no, cond, remark, slot_pos,
                     inspection_id, recognition_mode, timing, elapsed_sec, token_usage, label_data)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
                (rec.get("operator") or "").strip(),
                (rec.get("sn") or "").strip(),
                (rec.get("brand") or "").strip(),
                (rec.get("model") or "").strip(),
                (rec.get("frequency") or "").strip(),
                (rec.get("spec") or "").strip()[:128],
                (rec.get("mfg") or "").strip()[:64],
                rec.get("controller_date") or None,
                rec.get("pcb_date") or None,
                json.dumps(chips, ensure_ascii=False),
                len(chips),
                _tri(rec.get("comp_ok")),
                _tri(rec.get("gold_finger_ok")),
                _tri(rec.get("chip_mark_ok")),
                _tri(rec.get("date_ok")),
                rec.get("verdict") or "",
                (rec.get("fail_desc") or "")[:255],
                rec.get("review_status") or "未复查",
                (rec.get("front_img") or "")[:255],
                (rec.get("back_img") or "")[:255],
                (rec.get("annotated_front") or "")[:255],
                (rec.get("annotated_back") or "")[:255],
                (rec.get("customer") or "").strip()[:128],
                (rec.get("supplier") or "").strip()[:128],
                (rec.get("capacity") or "").strip()[:32],
                (rec.get("batch_no") or "").strip()[:64],
                (rec.get("cond") or "").strip()[:16],
                (rec.get("remark") or "").strip()[:255],
                rec.get("slot_pos") if rec.get("slot_pos") else None,
                (rec.get("inspection_id") or "").strip()[:32],
                (rec.get("recognition_mode") or "rules").strip()[:16],
                json.dumps(rec.get("timing") or {}, ensure_ascii=False),
                float(rec.get("elapsed_sec") or 0),
                json.dumps(rec.get("token_usage") or {}, ensure_ascii=False),
                json.dumps(rec.get("label_data") or {}, ensure_ascii=False),
            ))
            rid = cur.lastrowid
        metrics.record_verdict(rec.get("verdict") or "")
        log.info("保存质检记录 #%s verdict=%s sn=%s", rid, rec.get("verdict"), rec.get("sn"))
        return rid
    finally:
        conn.close()


def update_record_runtime(record_ids: list[int], timing: dict, token_usage: dict,
                          elapsed_sec: float) -> None:
    """检测结束后回填完整墙钟耗时和单次 token；同一盘的多条记录保持一致。"""
    ids = [int(v) for v in record_ids if v]
    if not ids:
        return
    conn = _connect()
    try:
        with conn.cursor() as cur:
            marks = ",".join(["%s"] * len(ids))
            cur.execute(
                f"UPDATE inspection_records SET timing=%s, token_usage=%s, elapsed_sec=%s "
                f"WHERE id IN ({marks})",
                (json.dumps(timing or {}, ensure_ascii=False),
                 json.dumps(token_usage or {}, ensure_ascii=False),
                 float(elapsed_sec or 0), *ids),
            )
    finally:
        conn.close()


def update_review(record_id: int, status: str) -> bool:
    if status not in ("未复查", "复查合格", "复查不合格"):
        return False
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE inspection_records SET review_status=%s WHERE id=%s",
                        (status, int(record_id)))
            return cur.rowcount > 0
    finally:
        conn.close()


_REC_SELECT = """
    SELECT id, created_at, operator, sn, brand, model, frequency,
           spec, mfg,
           controller_date, pcb_date, storage_chips, storage_count,
           comp_ok, gold_finger_ok, chip_mark_ok, date_ok,
           verdict, fail_desc, review_status,
           front_img, back_img, annotated_front, annotated_back,
           customer, supplier, capacity, batch_no, cond, remark, slot_pos,
           inspection_id, recognition_mode, timing, elapsed_sec, token_usage, label_data
    FROM inspection_records"""


def _finish_rows(rows: list[dict]) -> list[dict]:
    """记录行后处理：JSON 解析 + created_at 格式化。"""
    for r in rows:
        for field, fallback in (("storage_chips", None), ("timing", {}),
                                ("token_usage", {}), ("label_data", {})):
            try:
                value = r.get(field)
                r[field] = value if isinstance(value, (dict, list)) else json.loads(value or "null")
                if r[field] is None and fallback is not None:
                    r[field] = fallback
            except Exception:
                r[field] = fallback
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
    return rows


def _query_records(where_sql: str, params: list) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(_REC_SELECT + where_sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return _finish_rows(rows)


def list_records(limit: int = 50) -> list[dict]:
    return _query_records(" ORDER BY id DESC LIMIT %s", [int(limit)])


def list_records_by_sn(sn: str) -> list[dict]:
    """某个 SN 的全部质检记录（按时间倒序），用于追溯 + 同 SN 历史比对。"""
    return _query_records(" WHERE sn=%s ORDER BY id DESC LIMIT 200", [(sn or "").strip()])


def list_records_filtered(customer=None, batch_no=None, verdict=None,
                          date_from=None, date_to=None, limit=2000) -> list[dict]:
    """按 客户/批次/判定/日期 过滤（用于报表导出）。空条件即忽略。"""
    where, params = [], []
    if customer:
        where.append("customer=%s"); params.append(customer)
    if batch_no:
        where.append("batch_no=%s"); params.append(batch_no)
    if verdict:
        where.append("verdict=%s"); params.append(verdict)
    if date_from:
        where.append("created_at>=%s"); params.append(date_from)
    if date_to:
        where.append("created_at<=%s")
        params.append(date_to + " 23:59:59" if len(str(date_to)) == 10 else date_to)
    where_sql = (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT %s"
    params.append(int(limit))
    return _query_records(where_sql, params)


# ----------------------------- 批次登记 -----------------------------

def create_batch(batch_no: str, customer: str, brand: str,
                 capacity: str, frequency: str, cond: str = "", remark: str = "",
                 model: str = "", supplier: str = "", qty_expected: int = 0,
                 delivery_date: str = "", oa_order_no: str = "",
                 oa_synced: int = 0, oa_raw=None,
                 kd_bill_status: str = "", kd_close_status: str = "",
                 kd_pay_mode: str = "", kd_operator: str = "",
                 kd_supplier_number: str = "", kd_supplier_status: str = "",
                 kd_material_number: str = "", kd_src_bill_no: str = "",
                 kd_specification: str = "",
                 kd_total_amount: float = 0, kd_total_all_amount: float = 0,
                 kd_tax_amount: float = 0, kd_received_qty: float = 0,
                 kd_in_stock_qty: float = 0, kd_return_qty: float = 0) -> int:
    """登记一个采购订单（底层表仍叫 batch），返回自增 id。

    字段：批次号/客户/品牌/容量/频率/品相/备注(来源) + 型号/供应商/应检数量/交期/OA订单号。
    **幂等**：批次号非空且已有同号活动订单时，更新其信息并复用原 id（不重复登记）——
    防"同一订单号重复登记/重复点击"造出一堆重复订单。
    """
    bn = (batch_no or "").strip()[:64]
    dd = (delivery_date or "").strip() or None            # 空字符串 → NULL，避免 DATE 报错
    raw = json.dumps(oa_raw, ensure_ascii=False) if oa_raw is not None else None
    # 与 UPDATE / INSERT 列顺序一致的公共值
    vals = ((customer or "").strip()[:128], (brand or "").strip()[:64],
            (capacity or "").strip()[:32], (frequency or "").strip()[:32],
            (cond or "").strip()[:16], (remark or "").strip()[:255],
            (model or "").strip()[:128], (supplier or "").strip()[:128],
            int(qty_expected or 0), dd, (oa_order_no or "").strip()[:64],
            1 if oa_synced else 0, raw,
            (kd_bill_status or "").strip()[:16], (kd_close_status or "").strip()[:16],
            (kd_pay_mode or "").strip()[:32], (kd_operator or "").strip()[:64],
            (kd_supplier_number or "").strip()[:64], (kd_supplier_status or "").strip()[:32],
            (kd_material_number or "").strip()[:64], (kd_src_bill_no or "").strip()[:64],
            (kd_specification or "").strip()[:128],
            float(kd_total_amount or 0), float(kd_total_all_amount or 0),
            float(kd_tax_amount or 0), float(kd_received_qty or 0),
            float(kd_in_stock_qty or 0), float(kd_return_qty or 0))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if bn:                                     # 有订单号 → 查同号活动订单，有则复用
                cur.execute("SELECT id FROM batches WHERE batch_no=%s AND active=1 ORDER BY id LIMIT 1", (bn,))
                row = cur.fetchone()
                if row:
                    bid = row[0]
                    cur.execute("""UPDATE batches SET customer=%s,brand=%s,capacity=%s,
                                   frequency=%s,cond=%s,remark=%s,model=%s,supplier=%s,
                                   qty_expected=%s,delivery_date=%s,oa_order_no=%s,
                                   oa_synced=%s,oa_raw=%s,kd_bill_status=%s,
                                   kd_close_status=%s,kd_pay_mode=%s,kd_operator=%s,
                                   kd_supplier_number=%s,kd_supplier_status=%s,
                                   kd_material_number=%s,kd_src_bill_no=%s,
                                   kd_specification=%s,
                                   kd_total_amount=%s,kd_total_all_amount=%s,kd_tax_amount=%s,
                                   kd_received_qty=%s,kd_in_stock_qty=%s,kd_return_qty=%s
                                   WHERE id=%s""", (*vals, bid))
                    log.info("复用已有订单 #%s no=%s（更新信息，不重复登记）", bid, bn)
                    return bid
            cur.execute("""INSERT INTO batches(batch_no, customer, brand, capacity, frequency,
                           cond, remark, model, supplier, qty_expected, delivery_date,
                           oa_order_no, oa_synced, oa_raw, kd_bill_status, kd_close_status,
                           kd_pay_mode, kd_operator, kd_supplier_number, kd_supplier_status,
                           kd_material_number, kd_src_bill_no, kd_specification,
                           kd_total_amount, kd_total_all_amount, kd_tax_amount,
                           kd_received_qty, kd_in_stock_qty, kd_return_qty, active)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)""", (bn, *vals))
            bid = cur.lastrowid
        log.info("登记订单 #%s no=%s 客户=%s 品牌=%s 应检=%s", bid, bn, vals[0], vals[1], vals[8])
        return bid
    finally:
        conn.close()


def get_batch(batch_id: int) -> dict | None:
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 带已检/良品数（LEFT JOIN 记录按 batch_no），与 list_batches 一致，供工作台顶栏实时进度
            cur.execute("SELECT b.id, b.created_at, b.batch_no, b.customer, b.brand, b.capacity, "
                        "b.frequency, b.cond, b.remark, b.model, b.supplier, b.qty_expected, "
                        "b.delivery_date, b.oa_order_no, b.oa_synced, b.kd_bill_status, "
                        "b.kd_close_status, b.kd_pay_mode, b.kd_operator, "
                        "b.kd_supplier_number, b.kd_supplier_status, b.kd_material_number, "
                        "b.kd_src_bill_no, b.kd_specification, "
                        "b.kd_total_amount, b.kd_total_all_amount, "
                        "b.kd_tax_amount, b.kd_received_qty, b.kd_in_stock_qty, "
                        "b.kd_return_qty, b.active, "
                        "COUNT(r.id) AS total, COALESCE(SUM(r.verdict='pass'),0) AS passed "
                        "FROM batches b "
                        "LEFT JOIN inspection_records r ON r.batch_no=b.batch_no AND b.batch_no<>'' "
                        "WHERE b.id=%s GROUP BY b.id", (int(batch_id),))
            r = cur.fetchone()
    finally:
        conn.close()
    if r:
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if r.get("delivery_date") is not None:
            r["delivery_date"] = r["delivery_date"].strftime("%Y-%m-%d")
        t, p = int(r.get("total") or 0), int(r.get("passed") or 0)
        r["total"], r["passed"] = t, p
        r["passed_rate"] = round(p / t, 4) if t else None
        q = int(r.get("qty_expected") or 0)
        r["qty_expected"] = q
        r["qty_remain"] = (q - t) if q else None       # 应检-已检差额，未填应检=None
        for k in ("kd_total_amount", "kd_total_all_amount", "kd_tax_amount",
                  "kd_received_qty", "kd_in_stock_qty", "kd_return_qty"):
            r[k] = float(r.get(k) or 0)
    return r


def close_batch(batch_id: int) -> bool:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE batches SET active=0 WHERE id=%s", (int(batch_id),))
            return cur.rowcount > 0
    finally:
        conn.close()


def list_batches(limit: int = 100) -> list[dict]:
    """批次列表 + 每批已检/良品数（LEFT JOIN 记录按 batch_no）。供看板与"当前批次"下拉。"""
    conn = _connect()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("""
                SELECT b.id, b.created_at, b.batch_no, b.customer, b.brand, b.capacity,
                       b.frequency, b.cond, b.remark, b.model, b.supplier,
                       b.qty_expected, b.delivery_date, b.oa_order_no, b.oa_synced, b.active,
                       b.kd_bill_status, b.kd_close_status, b.kd_pay_mode, b.kd_operator,
                       b.kd_supplier_number, b.kd_supplier_status,
                       b.kd_material_number, b.kd_src_bill_no, b.kd_specification,
                       b.kd_total_amount,
                       b.kd_total_all_amount, b.kd_tax_amount, b.kd_received_qty,
                       b.kd_in_stock_qty, b.kd_return_qty,
                       COUNT(r.id) AS total,
                       COALESCE(SUM(r.verdict='pass'),0) AS passed
                FROM batches b
                LEFT JOIN inspection_records r
                       ON r.batch_no=b.batch_no AND b.batch_no<>''
                GROUP BY b.id ORDER BY b.id DESC LIMIT %s""", (int(limit),))
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        if r.get("delivery_date") is not None:
            r["delivery_date"] = r["delivery_date"].strftime("%Y-%m-%d")
        t, p = int(r.get("total") or 0), int(r.get("passed") or 0)
        r["total"], r["passed"] = t, p
        r["yield"] = round(p / t, 4) if t else None
        q = int(r.get("qty_expected") or 0)
        r["qty_expected"] = q
        r["qty_remain"] = (q - t) if q else None       # 应检-已检差额，未填应检=None
        for k in ("kd_total_amount", "kd_total_all_amount", "kd_tax_amount",
                  "kd_received_qty", "kd_in_stock_qty", "kd_return_qty"):
            r[k] = float(r.get(k) or 0)
    return rows


# ----------------------------- 良率统计（看板）-----------------------------

def _rate(total: int, passed: int):
    return round(passed / total, 4) if total else None


def yield_overview() -> dict:
    """今日 / 累计 良率（verdict='pass' 为良品）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COALESCE(SUM(verdict='pass'),0) FROM inspection_records")
            at, ap = cur.fetchone()
            cur.execute("SELECT COUNT(*), COALESCE(SUM(verdict='pass'),0) "
                        "FROM inspection_records WHERE DATE(created_at)=CURDATE()")
            tt, tp = cur.fetchone()
    finally:
        conn.close()
    at, ap, tt, tp = int(at), int(ap), int(tt), int(tp)
    return {"today": {"total": tt, "passed": tp, "yield": _rate(tt, tp)},
            "all": {"total": at, "passed": ap, "yield": _rate(at, ap)}}


def customer_stats() -> list[dict]:
    """按客户汇总良率。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT customer, COUNT(*), COALESCE(SUM(verdict='pass'),0)
                           FROM inspection_records WHERE customer<>''
                           GROUP BY customer ORDER BY COUNT(*) DESC""")
            rows = cur.fetchall()
    finally:
        conn.close()
    return [{"customer": c, "total": int(t), "passed": int(p), "yield": _rate(int(t), int(p))}
            for c, t, p in rows]
