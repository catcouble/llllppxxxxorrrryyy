import asyncio
import json
import logging
import uuid
import re
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional, Set
from dataclasses import dataclass, field, asdict
from enum import Enum
import signal
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.responses import StreamingResponse
import typing
import os
import traceback
from datetime import datetime, timedelta
from collections import defaultdict, deque
import threading
from pathlib import Path
import aiohttp  # 如果用于发送HTTP告警，需要先 pip install aiohttp
import gzip
import shutil
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

# --- Configuration ---
# 配置项集中管理
class Config:
    # 日志配置
    LOG_DIR = Path("logs")
    REQUEST_LOG_FILE = "requests.jsonl"
    ERROR_LOG_FILE = "errors.jsonl"
    MAX_LOG_SIZE = 50 * 1024 * 1024  # 50MB
    MAX_LOG_FILES = 50  # 保留最多10个历史日志文件
    
    # 服务器配置
    HOST = "0.0.0.0"
    PORT = 9080
    
    # 请求配置
    BACKPRESSURE_QUEUE_SIZE = 5
    REQUEST_TIMEOUT_SECONDS = 180
    MAX_CONCURRENT_REQUESTS = 20  # 最大并发请求数
    
    # 监控配置
    STATS_UPDATE_INTERVAL = 5  # 统计更新间隔（秒）
    CLEANUP_INTERVAL = 300  # 清理间隔（秒）
    
    # 内存限制
    MAX_ACTIVE_REQUESTS = 100
    MAX_LOG_MEMORY_ITEMS = 1000  # 内存中保留的最大日志条目
    MAX_REQUEST_DETAILS = 500  # 保留的请求详情数量

    # 网络配置
    MANUAL_IP = None  # 手动指定IP地址，如 "192.168.0.1"

# 确保日志目录存在
Config.LOG_DIR.mkdir(exist_ok=True)

# --- 动态配置管理 ---
class ConfigManager:
    """管理可动态修改的配置"""

    def __init__(self):
        self.config_file = Config.LOG_DIR / "config.json"
        self.dynamic_config = {
            "network": {
                "manual_ip": Config.MANUAL_IP,
                "port": Config.PORT,
                "auto_detect_ip": True
            },
            "request": {
                "timeout_seconds": Config.REQUEST_TIMEOUT_SECONDS,
                "max_concurrent_requests": Config.MAX_CONCURRENT_REQUESTS,
                "backpressure_queue_size": Config.BACKPRESSURE_QUEUE_SIZE
            },
            "quick_links": [
                {"name": "监控面板", "url": "/monitor", "icon": "📊"},
                {"name": "健康检查", "url": "/health", "icon": "🏥"}, 
                {"name": "Prometheus", "url": "/metrics", "icon": "📈"},
                {"name": "API文档", "url": "/monitor#api-docs", "icon": "📚"}
            ]
        }
        self.load_config()

    def load_config(self):
        """从文件加载配置"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    # 深度合并配置
                    self._deep_merge(self.dynamic_config, saved_config)
                    logging.info("已加载保存的配置")
            except Exception as e:
                logging.error(f"加载配置文件失败: {e}")

    def save_config(self):
        """保存配置到文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.dynamic_config, f, ensure_ascii=False, indent=2)
            logging.info("配置已保存")
        except Exception as e:
            logging.error(f"保存配置文件失败: {e}")

    def _deep_merge(self, target, source):
        """深度合并字典"""
        for key, value in source.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                self._deep_merge(target[key], value)
            else:
                target[key] = value

    def get(self, path: str, default=None):
        """获取配置值，支持点号路径如 'network.manual_ip'"""
        keys = path.split('.')
        value = self.dynamic_config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, path: str, value):
        """设置配置值"""
        keys = path.split('.')
        target = self.dynamic_config
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]
        target[keys[-1]] = value
        self.save_config()

    def get_display_ip(self):
        """获取显示用的IP地址"""
        if self.get('network.manual_ip'):
            return self.get('network.manual_ip')
        elif self.get('network.auto_detect_ip', True):
            return get_local_ip()
        else:
            return "localhost"


# 创建全局配置管理器
config_manager = ConfigManager()


def get_local_ip():
    """获取本机局域网IP地址"""
    import socket
    import platform

    # 检查是否有config_manager并且有手动配置的IP
    if 'config_manager' in globals():
        manual_ip = config_manager.get('network.manual_ip')
        if manual_ip:
            return manual_ip

    # 获取所有可能的IP地址
    ips = []

    try:
        # 方法1：获取所有网络接口的IP
        hostname = socket.gethostname()
        all_ips = socket.gethostbyname_ex(hostname)[2]

        # 过滤出局域网IP（排除虚拟网卡）
        for ip in all_ips:
            # 排除回环地址和Clash虚拟网卡地址
            if not ip.startswith('127.') and not ip.startswith('198.18.'):
                # 检查是否是私有IP地址
                parts = ip.split('.')
                if len(parts) == 4:
                    first_octet = int(parts[0])
                    second_octet = int(parts[1])

                    # 检查是否是私有IP范围
                    # 10.0.0.0 - 10.255.255.255
                    # 172.16.0.0 - 172.31.255.255
                    # 192.168.0.0 - 192.168.255.255
                    if (first_octet == 10 or
                            (first_octet == 172 and 16 <= second_octet <= 31) or
                            (first_octet == 192 and second_octet == 168)):
                        ips.append(ip)

        # 如果找到了局域网IP，返回第一个（通常是最主要的）
        if ips:
            # 优先返回192.168开头的地址
            for ip in ips:
                if ip.startswith('192.168.'):
                    return ip
            return ips[0]

        # 方法2：如果上面的方法失败，尝试连接外部服务器的方法
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))  # 使用阿里DNS而不是Google
        local_ip = s.getsockname()[0]
        s.close()

        # 再次检查是否是Clash地址
        if not local_ip.startswith('198.18.'):
            return local_ip

    except Exception as e:
        logging.warning(f"获取IP地址失败: {e}")

    # 如果所有方法都失败，返回localhost
    return "127.0.0.1"

# 配置Python日志系统
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(),  # 控制台输出
        logging.FileHandler(Config.LOG_DIR / "server.log", encoding='utf-8')  # 文件输出
    ]
)

# --- Prometheus Metrics ---
# 请求计数器
request_count = Counter(
    'lmarena_requests_total', 
    'Total number of requests',
    ['model', 'status', 'type']
)

# 请求持续时间直方图
request_duration = Histogram(
    'lmarena_request_duration_seconds',
    'Request duration in seconds',
    ['model', 'type'],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, float("inf"))
)

# 活跃请求数量
active_requests_gauge = Gauge(
    'lmarena_active_requests',
    'Number of active requests'
)

# Token使用计数器
token_usage = Counter(
    'lmarena_tokens_total',
    'Total number of tokens used',
    ['model', 'token_type']  # token_type: input/output
)

# WebSocket连接状态
websocket_status = Gauge(
    'lmarena_websocket_connected',
    'WebSocket connection status (1=connected, 0=disconnected)'
)

# 错误计数器
error_count = Counter(
    'lmarena_errors_total',
    'Total number of errors',
    ['error_type', 'model']
)

# 模型注册数量
model_registry_gauge = Gauge(
    'lmarena_models_registered',
    'Number of registered models'
)

# --- Request Details Storage ---
@dataclass
class RequestDetails:
    """存储请求的详细信息"""
    request_id: str
    timestamp: float
    model: str
    status: str
    duration: float
    input_tokens: int
    output_tokens: int
    error: Optional[str]
    request_params: dict
    request_messages: list
    response_content: str
    headers: dict
    
class RequestDetailsStorage:
    """管理请求详情的存储"""
    def __init__(self, max_size: int = Config.MAX_REQUEST_DETAILS):
        self.details: Dict[str, RequestDetails] = {}
        self.order: deque = deque(maxlen=max_size)
        self._lock = threading.Lock()
    
    def add(self, details: RequestDetails):
        """添加请求详情"""
        with self._lock:
            if details.request_id in self.details:
                return
            
            # 如果达到最大容量，删除最旧的
            if len(self.order) >= self.order.maxlen:
                oldest_id = self.order[0]
                if oldest_id in self.details:
                    del self.details[oldest_id]
            
            self.details[details.request_id] = details
            self.order.append(details.request_id)
    
    def get(self, request_id: str) -> Optional[RequestDetails]:
        """获取请求详情"""
        with self._lock:
            return self.details.get(request_id)
    
    def get_recent(self, limit: int = 100) -> list:
        """获取最近的请求详情"""
        with self._lock:
            recent_ids = list(self.order)[-limit:]
            return [self.details[id] for id in reversed(recent_ids) if id in self.details]

# 创建请求详情存储
request_details_storage = RequestDetailsStorage()

# --- 日志管理器 ---
class LogManager:
    """管理JSON Lines格式的日志文件"""
    
    def __init__(self):
        self.request_log_path = Config.LOG_DIR / Config.REQUEST_LOG_FILE
        self.error_log_path = Config.LOG_DIR / Config.ERROR_LOG_FILE
        self._lock = threading.Lock()
        self._check_and_rotate()
    
    def _check_and_rotate(self):
        """检查并轮转日志文件"""
        for log_path in [self.request_log_path, self.error_log_path]:
            if log_path.exists() and log_path.stat().st_size > Config.MAX_LOG_SIZE:
                self._rotate_log(log_path)
    
    def _rotate_log(self, log_path: Path):
        """轮转日志文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rotated_path = log_path.with_suffix(f".{timestamp}.jsonl")
        
        # 移动当前日志文件
        shutil.move(log_path, rotated_path)
        
        # 压缩旧日志
        with open(rotated_path, 'rb') as f_in:
            with gzip.open(f"{rotated_path}.gz", 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        
        # 删除未压缩的文件
        rotated_path.unlink()
        
        # 清理旧日志文件
        self._cleanup_old_logs()
    
    def _cleanup_old_logs(self):
        """清理旧的日志文件"""
        log_files = sorted(Config.LOG_DIR.glob("*.jsonl.gz"), key=lambda x: x.stat().st_mtime)
        
        # 保留最新的N个文件
        while len(log_files) > Config.MAX_LOG_FILES:
            oldest_file = log_files.pop(0)
            oldest_file.unlink()
            logging.info(f"删除旧日志文件: {oldest_file}")
    
    def write_request_log(self, log_entry: dict):
        """写入请求日志"""
        with self._lock:
            self._check_and_rotate()
            with open(self.request_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    
    def write_error_log(self, log_entry: dict):
        """写入错误日志"""
        with self._lock:
            self._check_and_rotate()
            with open(self.error_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    
    def read_request_logs(self, limit: int = 100, offset: int = 0, model: str = None) -> list:
        """读取请求日志"""
        logs = []
        
        # 读取当前日志文件
        if self.request_log_path.exists():
            with open(self.request_log_path, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()
                
                # 反向读取（最新的在前）
                for line in reversed(all_lines):
                    try:
                        log = json.loads(line.strip())
                        if log.get('type') == 'request_end':  # 只返回完成的请求
                            if model and log.get('model') != model:
                                continue
                            logs.append(log)
                            if len(logs) >= limit + offset:
                                break
                    except json.JSONDecodeError:
                        continue
        
        # 返回指定范围的日志
        return logs[offset:offset + limit]
    
    def read_error_logs(self, limit: int = 50) -> list:
        """读取错误日志"""
        logs = []
        
        if self.error_log_path.exists():
            with open(self.error_log_path, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()
                
                # 反向读取（最新的在前）
                for line in reversed(all_lines[-limit:]):
                    try:
                        log = json.loads(line.strip())
                        logs.append(log)
                    except json.JSONDecodeError:
                        continue
        
        return logs

# 创建全局日志管理器
log_manager = LogManager()

# --- 性能监控 ---
class PerformanceMonitor:
    """简化的性能监控器"""
    
    def __init__(self):
        self.request_times = deque(maxlen=1000)
        self.model_stats = defaultdict(lambda: {'count': 0, 'errors': 0})
    
    def record_request(self, model: str, duration: float, success: bool):
        """记录请求性能"""
        self.request_times.append(duration)
        self.model_stats[model]['count'] += 1
        if not success:
            self.model_stats[model]['errors'] += 1
    
    def get_stats(self) -> dict:
        """获取基本统计"""
        if not self.request_times:
            return {'avg_response_time': 0}
        return {
            'avg_response_time': sum(self.request_times) / len(self.request_times)
        }


# 创建性能监控器
performance_monitor = PerformanceMonitor()

# --- WebSocket心跳管理 ---
class WebSocketHeartbeat:
    def __init__(self, interval: int = 30):
        self.interval = interval
        self.last_ping = time.time()
        self.last_pong = time.time()
        self.missed_pongs = 0
        self.max_missed_pongs = 3

    async def start_heartbeat(self, ws: WebSocket):
        """启动心跳任务"""
        while not SHUTTING_DOWN and ws:
            try:
                current_time = time.time()

                # 检查是否收到pong响应
                if current_time - self.last_pong > self.interval * 2:
                    self.missed_pongs += 1
                    if self.missed_pongs >= self.max_missed_pongs:
                        logging.warning("心跳超时，浏览器可能已断线")
                        await self.notify_disconnect()
                        break

                # 发送ping
                await ws.send_text(json.dumps({"type": "ping", "timestamp": current_time}))
                self.last_ping = current_time

                await asyncio.sleep(self.interval)

            except Exception as e:
                logging.error(f"心跳发送失败: {e}")
                break

    def handle_pong(self):
        """处理pong响应"""
        self.last_pong = time.time()
        self.missed_pongs = 0

    async def notify_disconnect(self):
        """通知监控面板连接断开"""
        logging.warning("浏览器WebSocket心跳超时")




# --- 实时统计数据结构 ---
@dataclass
class RealtimeStats:
    active_requests: Dict[str, dict] = field(default_factory=dict)
    recent_requests: deque = field(default_factory=lambda: deque(maxlen=Config.MAX_LOG_MEMORY_ITEMS))
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=50))
    model_usage: Dict[str, dict] = field(default_factory=lambda: defaultdict(lambda: {
        'requests': 0, 'tokens': 0, 'errors': 0, 'avg_duration': 0
    }))
    
    def cleanup_old_requests(self):
        """清理超时的活跃请求"""
        current_time = time.time()
        timeout_requests = []
        
        for req_id, req in self.active_requests.items():
            if current_time - req['start_time'] > Config.REQUEST_TIMEOUT_SECONDS:
                timeout_requests.append(req_id)
        
        for req_id in timeout_requests:
            logging.warning(f"清理超时请求: {req_id}")
            del self.active_requests[req_id]

realtime_stats = RealtimeStats()

# --- 清理任务 ---
async def periodic_cleanup():
    """定期清理任务"""
    while not SHUTTING_DOWN:
        try:
            # 清理超时的活跃请求
            realtime_stats.cleanup_old_requests()
            
            # 触发日志轮转检查
            log_manager._check_and_rotate()
            
            # 更新Prometheus指标
            active_requests_gauge.set(len(realtime_stats.active_requests))
            model_registry_gauge.set(len(MODEL_REGISTRY))
            
            logging.info(f"清理任务执行完成. 活跃请求: {len(realtime_stats.active_requests)}")
            
        except Exception as e:
            logging.error(f"清理任务出错: {e}")
        
        await asyncio.sleep(Config.CLEANUP_INTERVAL)

# --- Custom Streaming Response with Immediate Flush ---
class ImmediateStreamingResponse(StreamingResponse):
    """Custom streaming response that forces immediate flushing of chunks"""

    async def stream_response(self, send: typing.Callable) -> None:
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": self.raw_headers,
        })

        async for chunk in self.body_iterator:
            if chunk:
                # Send the chunk immediately
                await send({
                    "type": "http.response.body",
                    "body": chunk.encode(self.charset) if isinstance(chunk, str) else chunk,
                    "more_body": True,
                })
                # Force a small delay to ensure the chunk is sent
                await asyncio.sleep(0)

        # Send final empty chunk to close the stream
        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        })


# --- Request State Management ---
class RequestStatus(Enum):
    PENDING = "pending"
    SENT_TO_BROWSER = "sent_to_browser"
    PROCESSING = "processing"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class PersistentRequest:
    request_id: str
    openai_request: dict
    response_queue: asyncio.Queue
    status: RequestStatus = RequestStatus.PENDING
    created_at: float = field(default_factory=time.time)
    sent_to_browser_at: Optional[float] = None
    last_activity_at: Optional[float] = None
    model_name: str = ""
    is_streaming: bool = True
    accumulated_response: str = ""  # 存储响应内容


class PersistentRequestManager:
    def __init__(self):
        self.active_requests: Dict[str, PersistentRequest] = {}
        self._lock = asyncio.Lock()

    async def add_request(self, request_id: str, openai_request: dict, response_queue: asyncio.Queue,
                    model_name: str, is_streaming: bool) -> PersistentRequest:
        """Add a new request to be tracked"""
        async with self._lock:
            # 检查是否超过最大并发数
            if len(self.active_requests) >= Config.MAX_CONCURRENT_REQUESTS:
                raise HTTPException(status_code=503, detail="Too many concurrent requests")
            
            persistent_req = PersistentRequest(
                request_id=request_id,
                openai_request=openai_request,
                response_queue=response_queue,
                model_name=model_name,
                is_streaming=is_streaming
            )
            self.active_requests[request_id] = persistent_req
            
            # 更新Prometheus指标
            active_requests_gauge.inc()
            
            logging.info(f"REQUEST_MGR: Added request {request_id} for tracking")
            return persistent_req

    def get_request(self, request_id: str) -> Optional[PersistentRequest]:
        """Get a request by ID"""
        return self.active_requests.get(request_id)

    def update_status(self, request_id: str, status: RequestStatus):
        """Update request status"""
        if request_id in self.active_requests:
            self.active_requests[request_id].status = status
            self.active_requests[request_id].last_activity_at = time.time()
            logging.debug(f"REQUEST_MGR: Updated request {request_id} status to {status.value}")

    def mark_sent_to_browser(self, request_id: str):
        """Mark request as sent to browser"""
        if request_id in self.active_requests:
            self.active_requests[request_id].sent_to_browser_at = time.time()
            self.update_status(request_id, RequestStatus.SENT_TO_BROWSER)

    async def timeout_request(self, request_id: str):
        """Timeout a request and send error to client"""
        if request_id in self.active_requests:
            req = self.active_requests[request_id]
            req.status = RequestStatus.TIMEOUT

            # Send timeout error to client
            try:
                await req.response_queue.put({
                    "error": f"Request timed out after {Config.REQUEST_TIMEOUT_SECONDS} seconds. Browser may have disconnected during Cloudflare challenge."
                })
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logging.error(f"REQUEST_MGR: Error sending timeout to queue for {request_id}: {e}")

            # Remove from active requests
            del self.active_requests[request_id]
            active_requests_gauge.dec()
            logging.warning(f"REQUEST_MGR: Request {request_id} timed out and removed")

    def complete_request(self, request_id: str):
        """Mark request as completed and remove from tracking"""
        if request_id in self.active_requests:
            self.active_requests[request_id].status = RequestStatus.COMPLETED
            del self.active_requests[request_id]
            active_requests_gauge.dec()
            logging.info(f"REQUEST_MGR: Request {request_id} completed and removed")

    def get_pending_requests(self) -> Dict[str, PersistentRequest]:
        """Get all requests that were sent to browser but not completed"""
        return {
            req_id: req for req_id, req in self.active_requests.items()
            if req.status in [RequestStatus.SENT_TO_BROWSER, RequestStatus.PROCESSING]
        }

    async def request_timeout_watcher(self, requests_to_watch: Dict[str, PersistentRequest]):
        """A background task to watch for and time out disconnected requests."""
        try:
            await asyncio.sleep(Config.REQUEST_TIMEOUT_SECONDS)

            logging.info(f"WATCHER: Timeout reached. Checking {len(requests_to_watch)} requests.")
            for request_id, req in requests_to_watch.items():
                # Check if the request is still pending (i.e., not completed by a reconnect)
                if self.get_request(request_id) and req.status in [RequestStatus.SENT_TO_BROWSER,
                                                                   RequestStatus.PROCESSING]:
                    logging.warning(f"WATCHER: Request {request_id} timed out after browser disconnect.")
                    await self.timeout_request(request_id)
        except asyncio.CancelledError:
            logging.info("WATCHER: Request timeout watcher was cancelled, likely due to server shutdown.")
        except Exception as e:
            logging.error(f"WATCHER: Error in request timeout watcher: {e}", exc_info=True)

    async def handle_browser_disconnect(self):
        """Handle browser WebSocket disconnect - spawn timeout watchers for pending requests."""
        pending_requests = self.get_pending_requests()
        if not pending_requests:
            return

        logging.warning(f"REQUEST_MGR: Browser disconnected with {len(pending_requests)} pending requests.")

        # Check if we're shutting down
        global SHUTTING_DOWN
        if SHUTTING_DOWN:
            # During shutdown, timeout immediately to avoid hanging
            logging.info("REQUEST_MGR: Server shutting down, timing out all pending requests immediately.")
            for request_id in list(pending_requests.keys()):
                logging.info(f"REQUEST_MGR: Timing out request {request_id} due to shutdown.")
                await self.timeout_request(request_id)
        else:
            # During normal operation, spawn a watcher task for the timeout
            logging.info(f"REQUEST_MGR: Spawning timeout watcher for {len(pending_requests)} pending requests.")
            watcher_task = asyncio.create_task(self.request_timeout_watcher(pending_requests.copy()))
            background_tasks.add(watcher_task)
            watcher_task.add_done_callback(background_tasks.discard)


# --- Logging Functions ---
def log_request_start(request_id: str, model: str, params: dict, messages: list = None):
    """记录请求开始"""
    request_info = {
        'id': request_id,
        'model': model,
        'start_time': time.time(),
        'status': 'active',
        'params': params,
        'messages': messages or []
    }
    
    realtime_stats.active_requests[request_id] = request_info
    
    # 写入日志文件
    log_entry = {
        'type': 'request_start',
        'timestamp': time.time(),
        'request_id': request_id,
        'model': model,
        'params': params
    }
    log_manager.write_request_log(log_entry)
    
def log_request_end(request_id: str, success: bool, input_tokens: int = 0, 
                   output_tokens: int = 0, error: str = None, response_content: str = ""):
    """记录请求结束"""
    if request_id not in realtime_stats.active_requests:
        return
        
    req = realtime_stats.active_requests[request_id]
    duration = time.time() - req['start_time']
    
    # 更新实时统计
    req['status'] = 'success' if success else 'failed'
    req['duration'] = duration
    req['input_tokens'] = input_tokens
    req['output_tokens'] = output_tokens
    req['error'] = error
    req['end_time'] = time.time()
    req['response_content'] = response_content
    
    # 添加到最近请求列表
    realtime_stats.recent_requests.append(req.copy())
    
    # 更新模型统计
    model = req['model']
    stats = realtime_stats.model_usage[model]
    stats['requests'] += 1
    if success:
        stats['tokens'] += input_tokens + output_tokens
    else:
        stats['errors'] += 1
    
    # 记录性能
    performance_monitor.record_request(model, duration, success)
    
    # 更新Prometheus指标
    request_count.labels(model=model, status='success' if success else 'failed', type='chat').inc()
    request_duration.labels(model=model, type='chat').observe(duration)
    token_usage.labels(model=model, token_type='input').inc(input_tokens)
    token_usage.labels(model=model, token_type='output').inc(output_tokens)
    
    # 保存请求详情
    details = RequestDetails(
        request_id=request_id,
        timestamp=req['start_time'],
        model=model,
        status='success' if success else 'failed',
        duration=duration,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error=error,
        request_params=req.get('params', {}),
        request_messages=req.get('messages', []),
        response_content=response_content[:5000],  # 限制长度
        headers={}
    )
    request_details_storage.add(details)
    
    # 写入日志文件
    log_entry = {
        'type': 'request_end',
        'timestamp': time.time(),
        'request_id': request_id,
        'model': model,
        'status': 'success' if success else 'failed',
        'duration': duration,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'error': error,
        'params': req.get('params', {})
    }
    log_manager.write_request_log(log_entry)
    
    # 从活动请求中移除
    del realtime_stats.active_requests[request_id]

def log_error(request_id: str, error_type: str, error_message: str, stack_trace: str = ""):
    """记录错误日志"""
    error_data = {
        'timestamp': time.time(),
        'request_id': request_id,
        'error_type': error_type,
        'error_message': error_message,
        'stack_trace': stack_trace
    }
    
    realtime_stats.recent_errors.append(error_data)
    
    # 更新Prometheus指标
    model = realtime_stats.active_requests.get(request_id, {}).get('model', 'unknown')
    error_count.labels(error_type=error_type, model=model).inc()
    
    # 写入错误日志文件
    log_manager.write_error_log(error_data)

# --- Model Registry ---
MODEL_REGISTRY = {}  # Will be populated dynamically


def update_model_registry(models_data: dict) -> None:
    """Update the model registry with data from browser, inferring type from capabilities."""
    global MODEL_REGISTRY

    try:
        if not models_data or not isinstance(models_data, dict):
            logging.warning(f"Received empty or invalid model data: {models_data}")
            return

        new_registry = {}
        for public_name, model_info in models_data.items():
            if not isinstance(model_info, dict):
                continue

            # Determine type from outputCapabilities
            model_type = "chat"  # Default
            capabilities = model_info.get("capabilities", {})
            if isinstance(capabilities, dict):
                output_caps = capabilities.get("outputCapabilities", {})
                if isinstance(output_caps, dict):
                    if "image" in output_caps:
                        model_type = "image"
                    elif "video" in output_caps:
                        model_type = "video"

            # Store the processed model info with the determined type
            processed_info = model_info.copy()
            processed_info["type"] = model_type
            new_registry[public_name] = processed_info

        MODEL_REGISTRY = new_registry
        model_registry_gauge.set(len(MODEL_REGISTRY))
        logging.info(f"Updated and processed model registry with {len(MODEL_REGISTRY)} models.")

    except KeyboardInterrupt:
        raise
    except Exception as e:
        logging.error(f"Error updating model registry: {e}", exc_info=True)


def get_fallback_registry():
    """Fallback registry in case dynamic fetching fails."""
    return {}


# --- Global State ---
browser_ws: WebSocket | None = None
response_channels: dict[str, asyncio.Queue] = {}  # Keep for backward compatibility
request_manager = PersistentRequestManager()
background_tasks: Set[asyncio.Task] = set()
SHUTTING_DOWN = False
monitor_clients: Set[WebSocket] = set()  # 监控客户端连接
startup_time = time.time()  # 服务器启动时间

# --- Helper Functions for Monitoring ---
async def broadcast_to_monitors(data: dict):
    """向所有监控客户端广播数据"""
    if not monitor_clients:
        return
        
    disconnected = []
    for client in monitor_clients:
        try:
            await client.send_json(data)
        except:
            disconnected.append(client)
    
    # 清理断开的连接
    for client in disconnected:
        monitor_clients.discard(client)

# --- FastAPI App and Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global MODEL_REGISTRY, request_manager, startup_time
    logging.info(f"服务器正在启动...")
    startup_time = time.time()

    # 显示访问地址
    local_ip = get_local_ip()
    logging.info(f"🌐 Server access URLs:")
    logging.info(f"  - Local: http://localhost:{Config.PORT}")
    logging.info(f"  - Network: http://{local_ip}:{Config.PORT}")
    logging.info(f"📱 Use the Network URL to access from your phone on the same WiFi")

    # === 添加详细的端点说明 ===
    logging.info(f"\n📋 Available Endpoints:")
    logging.info(f"  🖥️  Monitor Dashboard: http://{local_ip}:{Config.PORT}/monitor")
    logging.info(f"     实时监控面板，查看系统状态、请求日志、性能指标")

    logging.info(f"\n  📊 Metrics & Health:")
    logging.info(f"     - Prometheus Metrics: http://{local_ip}:{Config.PORT}/metrics")
    logging.info(f"       Prometheus格式的性能指标，可接入Grafana")
    logging.info(f"     - Health Check: http://{local_ip}:{Config.PORT}/health")
    logging.info(f"       基础健康检查")

    logging.info(f"\n  🤖 AI API:")
    logging.info(f"     - Chat Completions: POST http://{local_ip}:{Config.PORT}/v1/chat/completions")
    logging.info(f"       OpenAI兼容的聊天API")
    logging.info(f"     - List Models: GET http://{local_ip}:{Config.PORT}/v1/models")
    logging.info(f"       获取可用模型列表")
    logging.info(f"     - Refresh Models: POST http://{local_ip}:{Config.PORT}/v1/refresh-models")
    logging.info(f"       刷新模型列表")

    logging.info(f"\n  📈 Statistics:")
    logging.info(f"     - Stats Summary: http://{local_ip}:{Config.PORT}/api/stats/summary")
    logging.info(f"       24小时统计摘要")
    logging.info(f"     - Request Logs: http://{local_ip}:{Config.PORT}/api/logs/requests")
    logging.info(f"       请求日志API")
    logging.info(f"     - Error Logs: http://{local_ip}:{Config.PORT}/api/logs/errors")
    logging.info(f"       错误日志API")
    logging.info(f"     - Alerts: http://{local_ip}:{Config.PORT}/api/alerts")
    logging.info(f"       系统告警历史")

    logging.info(f"\n  🛠️  OpenAI Client Config:")
    logging.info(f"     base_url='http://{local_ip}:{Config.PORT}/v1'")
    logging.info(f"     api_key='sk-any-string-you-like'")
    logging.info(f"\n{'=' * 60}\n")
    # === 结束添加 ===

    # Use fallback registry on startup - models will be updated by browser script
    MODEL_REGISTRY = get_fallback_registry()
    logging.info(f"已加载 {len(MODEL_REGISTRY)} 个备用模型")

    # 启动清理任务
    cleanup_task = asyncio.create_task(periodic_cleanup())
    background_tasks.add(cleanup_task)

    logging.info("服务器启动完成")

    try:
        yield
    finally:
        global SHUTTING_DOWN
        SHUTTING_DOWN = True
        logging.info(f"生命周期: 服务器正在关闭。正在取消 {len(background_tasks)} 个后台任务...")

        # Cancel all background tasks
        cancelled_tasks = []
        for task in list(background_tasks):
            if not task.done():
                logging.info(f"生命周期: 正在取消任务: {task}")
                task.cancel()
                cancelled_tasks.append(task)

        # Wait for cancelled tasks to finish
        if cancelled_tasks:
            logging.info(f"生命周期: 等待 {len(cancelled_tasks)} 个已取消的任务完成...")
            results = await asyncio.gather(*cancelled_tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logging.info(f"生命周期: 任务 {i} 完成，结果: {type(result).__name__}")
                else:
                    logging.info(f"生命周期: 任务 {i} 正常完成")

        logging.info("生命周期: 所有后台任务已取消。关闭完成。")


app = FastAPI(lifespan=lifespan)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发环境可以用*，生产环境建议改为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- WebSocket Handler (Producer) ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global browser_ws, request_manager
    await websocket.accept()
    logging.info("✅ 浏览器WebSocket已连接")
    browser_ws = websocket
    websocket_status.set(1)

    # 创建心跳实例并启动心跳任务
    heartbeat = WebSocketHeartbeat()
    heartbeat_task = asyncio.create_task(heartbeat.start_heartbeat(websocket))
    background_tasks.add(heartbeat_task)

    # Handle reconnection - check for pending requests
    pending_requests = request_manager.get_pending_requests()
    if pending_requests:
        logging.info(f"🔄 浏览器重连，有 {len(pending_requests)} 个待处理请求")

        # Send reconnection acknowledgment with pending request IDs
        await websocket.send_text(json.dumps({
            "type": "reconnection_ack",
            "pending_request_ids": list(pending_requests.keys()),
            "message": f"已重连。发现 {len(pending_requests)} 个待处理请求。"
        }))

    try:
        while True:
            message_str = await websocket.receive_text()
            message = json.loads(message_str)
            # 处理pong响应
            if message.get("type") == "pong":
                heartbeat.handle_pong()
                continue


            # Handle reconnection handshake from browser
            if message.get("type") == "reconnection_handshake":
                browser_pending_ids = message.get("pending_request_ids", [])
                logging.info(f"🤝 收到重连握手，浏览器有 {len(browser_pending_ids)} 个待处理请求")

                # Restore request channels for matching requests
                restored_count = 0
                for request_id in browser_pending_ids:
                    persistent_req = request_manager.get_request(request_id)
                    if persistent_req:
                        # Restore the response channel
                        response_channels[request_id] = persistent_req.response_queue
                        request_manager.update_status(request_id, RequestStatus.PROCESSING)
                        restored_count += 1
                        logging.info(f"🔄 已恢复请求通道: {request_id}")

                # Send restoration acknowledgment
                await websocket.send_text(json.dumps({
                    "type": "restoration_ack",
                    "restored_count": restored_count,
                    "message": f"已恢复 {restored_count} 个请求通道"
                }))
                continue

            # Handle model registry updates
            if message.get("type") == "model_registry":
                models_data = message.get("models", {})
                update_model_registry(models_data)

                # Send acknowledgment
                await websocket.send_text(json.dumps({
                    "type": "model_registry_ack",
                    "count": len(MODEL_REGISTRY)
                }))
                continue

            # Handle regular chat requests
            request_id = message.get("request_id")
            data = message.get("data")
            logging.debug(f"⬅️ 浏览器 [ID: {request_id}]: 收到数据: {data}")

            # Update request status to processing when we receive data
            if request_id:
                request_manager.update_status(request_id, RequestStatus.PROCESSING)

            # Handle both old and new request tracking systems
            if request_id in response_channels:
                queue = response_channels[request_id]
                logging.debug(f"浏览器 [ID: {request_id}]: 放入队列前大小: {queue.qsize()}")
                await queue.put(data)
                logging.debug(f"浏览器 [ID: {request_id}]: 数据已放入队列。新大小: {queue.qsize()}")

                # Check if this is the end of the request
                if data == "[DONE]":
                    request_manager.complete_request(request_id)

            else:
                # Check if this is a persistent request
                persistent_req = request_manager.get_request(request_id)
                if persistent_req:
                    logging.info(f"🔄 正在恢复持久请求的队列: {request_id}")
                    response_channels[request_id] = persistent_req.response_queue
                    await persistent_req.response_queue.put(data)
                    request_manager.update_status(request_id, RequestStatus.PROCESSING)

                    if data == "[DONE]":
                        request_manager.complete_request(request_id)
                else:
                    logging.warning(f"⚠️ 浏览器: 收到未知/已关闭的请求消息: {request_id}")

    except WebSocketDisconnect:
        logging.warning("❌ 浏览器客户端已断开连接")
    finally:
        browser_ws = None
        websocket_status.set(0)

        # Handle browser disconnect - keep persistent requests alive
        await request_manager.handle_browser_disconnect()

        # Only send errors to non-persistent requests
        for request_id, queue in response_channels.items():
            persistent_req = request_manager.get_request(request_id)
            if not persistent_req:  # Only error out non-persistent requests
                try:
                    await queue.put({"error": "Browser disconnected"})
                except KeyboardInterrupt:
                    raise
                except:
                    pass

        response_channels.clear()
        logging.info("WebSocket cleaned up. Persistent requests kept alive.")

# --- Monitor WebSocket ---
@app.websocket("/ws/monitor")
async def monitor_websocket(websocket: WebSocket):
    """监控面板的WebSocket连接"""
    await websocket.accept()
    monitor_clients.add(websocket)
    
    try:
        # 发送初始数据
        await websocket.send_json({
            "type": "initial_data",
            "active_requests": dict(realtime_stats.active_requests),
            "recent_requests": list(realtime_stats.recent_requests),
            "recent_errors": list(realtime_stats.recent_errors),
            "model_usage": dict(realtime_stats.model_usage)
        })
        
        while True:
            # 保持连接
            await websocket.receive_text()
            
    except WebSocketDisconnect:
        monitor_clients.remove(websocket)

# --- API Handler ---
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    global request_manager

    if not browser_ws:
        raise HTTPException(status_code=503, detail="Browser client not connected.")

    openai_req = await request.json()
    request_id = str(uuid.uuid4())
    is_streaming = openai_req.get("stream", True)
    model_name = openai_req.get("model")

    model_info = MODEL_REGISTRY.get(model_name)
    if not model_info:
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found.")
    model_type = model_info.get("type", "chat")

    # 添加请求开始日志
    request_params = {
        "temperature": openai_req.get("temperature"),
        "top_p": openai_req.get("top_p"),
        "max_tokens": openai_req.get("max_tokens"),
        "streaming": is_streaming
    }
    messages = openai_req.get("messages", [])
    log_request_start(request_id, model_name, request_params, messages)
    
    # 广播到监控客户端
    await broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": model_name,
        "timestamp": time.time()
    })

    # Create response queue and add to both systems for compatibility
    response_queue = asyncio.Queue(maxsize=Config.BACKPRESSURE_QUEUE_SIZE)
    response_channels[request_id] = response_queue

    # Add to persistent request manager
    try:
        persistent_req = await request_manager.add_request(
            request_id=request_id,
            openai_request=openai_req,
            response_queue=response_queue,
            model_name=model_name,
            is_streaming=is_streaming
        )
    except HTTPException:
        # 并发限制
        log_request_end(request_id, False, 0, 0, "Too many concurrent requests")
        raise

    logging.info(f"API [ID: {request_id}]: Created persistent request for model type '{model_type}'.")

    try:
        task = asyncio.create_task(send_to_browser_task(request_id, openai_req))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

        media_type = "text/event-stream" if is_streaming else "application/json"
        headers = {
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked"
        } if is_streaming else {}

        logging.info(f"API [ID: {request_id}]: Returning {media_type} response to client.")

        if is_streaming:
            # Use custom streaming response for immediate flush
            return ImmediateStreamingResponse(
                stream_generator(request_id, model_name, is_streaming=is_streaming, model_type=model_type),
                media_type=media_type,
                headers=headers
            )
        else:
            # Use regular response for non-streaming
            return StreamingResponse(
                stream_generator(request_id, model_name, is_streaming=is_streaming, model_type=model_type),
                media_type=media_type,
                headers=headers
            )
    except KeyboardInterrupt:
        # Clean up on keyboard interrupt
        if request_id in response_channels:
            del response_channels[request_id]
        request_manager.complete_request(request_id)
        raise
    except Exception as e:
        # Clean up both tracking systems
        if request_id in response_channels:
            del response_channels[request_id]
        request_manager.complete_request(request_id)
        logging.error(f"API [ID: {request_id}]: Exception: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def send_to_browser_task(request_id: str, openai_req: dict):
    """This task runs in the background, sending the request to the browser."""
    global request_manager

    if not browser_ws:
        logging.error(f"TASK [ID: {request_id}]: Cannot send, browser disconnected.")
        # Mark request as error if browser is not connected
        persistent_req = request_manager.get_request(request_id)
        if persistent_req:
            await persistent_req.response_queue.put({"error": "Browser not connected"})
        return

    try:
        lmarena_payload, files_to_upload = create_lmarena_request_body(openai_req)

        message_to_browser = {
            "request_id": request_id,
            "payload": lmarena_payload,
            "files_to_upload": files_to_upload
        }

        logging.info(f"TASK [ID: {request_id}]: Sending payload and {len(files_to_upload)} file(s) to browser.")
        await browser_ws.send_text(json.dumps(message_to_browser))

        # Mark as sent to browser in persistent request manager
        request_manager.mark_sent_to_browser(request_id)
        logging.info(f"TASK [ID: {request_id}]: Payload sent and marked as sent to browser.")

    except KeyboardInterrupt:
        raise
    except Exception as e:
        logging.error(f"Error creating or sending request body: {e}", exc_info=True)

        # Send error to both tracking systems
        if request_id in response_channels:
            await response_channels[request_id].put({"error": f"Failed to process request: {e}"})

        persistent_req = request_manager.get_request(request_id)
        if persistent_req:
            await persistent_req.response_queue.put({"error": f"Failed to process request: {e}"})
            request_manager.update_status(request_id, RequestStatus.ERROR)


# Simple token estimation function
def estimateTokens(text: str) -> int:
    """简单的token估算函数"""
    if not text:
        return 0
    # 粗略估算：平均每个token约4个字符
    return len(str(text)) // 4


# --- Stream Consumer ---
async def stream_generator(request_id: str, model: str, is_streaming: bool, model_type: str):
    global request_manager, browser_ws
    start_time = time.time()  # 添加开始时间记录

    # Get queue from either response_channels or persistent request
    queue = response_channels.get(request_id)
    persistent_req = request_manager.get_request(request_id)

    if not queue and persistent_req:
        queue = persistent_req.response_queue
        # Restore to response_channels for compatibility
        response_channels[request_id] = queue

    if not queue:
        logging.error(f"STREAMER [ID: {request_id}]: Queue not found!")
        return

    logging.info(f"STREAMER [ID: {request_id}]: Generator started for model type '{model_type}'.")
    await asyncio.sleep(0)

    response_id = f"chatcmpl-{uuid.uuid4()}"

    try:
        accumulated_content = ""
        media_urls = []
        finish_reason = None

        # Buffer for streaming chunks (minimum 50 chars)
        streaming_buffer = ""
        MIN_CHUNK_SIZE = 40
        last_chunk_time = time.time()
        MAX_BUFFER_TIME = 0.5  # Max 500ms before forcing flush

        while True:
            # Try to get data with a timeout to check buffer periodically
            try:
                raw_data = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                # No new data, but check if we should flush buffer
                if is_streaming and model_type == "chat" and streaming_buffer:
                    current_time = time.time()
                    if current_time - last_chunk_time >= MAX_BUFFER_TIME:
                        chunk = {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": streaming_buffer
                                },
                                "finish_reason": None
                            }],
                            "system_fingerprint": f"fp_{uuid.uuid4().hex[:8]}"
                        }
                        chunk_data = f"data: {json.dumps(chunk)}\n\n"
                        yield chunk_data
                        streaming_buffer = ""
                        last_chunk_time = current_time
                continue

            if raw_data == "[DONE]":
                break

            # Handle error dictionary from timeout or browser disconnect
            if isinstance(raw_data, dict) and "error" in raw_data:
                logging.error(f"STREAMER [ID: {request_id}]: Received error: {raw_data}")

                # Format error for OpenAI response
                openai_error = {
                    "error": {
                        "message": str(raw_data.get("error", "Unknown error")),
                        "type": "server_error",
                        "code": None
                    }
                }

                if is_streaming:
                    yield f"data: {json.dumps(openai_error)}\n\ndata: [DONE]\n\n"
                else:
                    yield json.dumps(openai_error)
                return

            # First, try to detect if this is a JSON error response from the server
            if isinstance(raw_data, str) and raw_data.strip().startswith('{'):
                try:
                    error_data = json.loads(raw_data.strip())
                    if "error" in error_data:
                        logging.error(f"STREAMER [ID: {request_id}]: Server returned error: {error_data}")

                        # Parse the actual error structure from the server
                        server_error = error_data["error"]

                        # If the server error is already in OpenAI format, use it directly
                        if isinstance(server_error, dict) and "message" in server_error:
                            openai_error = {"error": server_error}
                        else:
                            # If it's just a string, wrap it in OpenAI format
                            openai_error = {
                                "error": {
                                    "message": str(server_error),
                                    "type": "server_error",
                                    "code": None
                                }
                            }

                        if is_streaming:
                            yield f"data: {json.dumps(openai_error)}\n\ndata: [DONE]\n\n"
                        else:
                            yield json.dumps(openai_error)
                        return
                except json.JSONDecodeError:
                    pass  # Not a JSON error, continue with normal parsing

            # Skip processing if raw_data is not a string (e.g., error dict)
            if not isinstance(raw_data, str):
                logging.warning(f"STREAMER [ID: {request_id}]: Skipping non-string data: {type(raw_data)}")
                continue

            try:
                prefix, content = raw_data.split(":", 1)

                if model_type in ["image", "video"] and prefix == "a2":
                    media_data_list = json.loads(content)
                    for item in media_data_list:
                        url = item.get("image") if model_type == "image" else item.get("url")
                        if url:
                            logging.info(f"MEDIA [ID: {request_id}]: Found {model_type} URL: {url}")
                            media_urls.append(url)

                elif model_type == "chat" and prefix == "a0":
                    delta = json.loads(content)
                    if is_streaming:
                        # Add to buffer instead of sending immediately
                        streaming_buffer += delta

                        # Check if we should send: either buffer is full or timeout reached
                        current_time = time.time()
                        time_since_last = current_time - last_chunk_time

                        if len(streaming_buffer) >= MIN_CHUNK_SIZE or (
                                streaming_buffer and time_since_last >= MAX_BUFFER_TIME):
                            chunk = {
                                "id": response_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "content": streaming_buffer
                                    },
                                    "finish_reason": None
                                }],
                                "system_fingerprint": f"fp_{uuid.uuid4().hex[:8]}"
                            }
                            chunk_data = f"data: {json.dumps(chunk)}\n\n"
                            yield chunk_data
                            
                            # 累积内容用于请求详情
                            accumulated_content += streaming_buffer
                            
                            # Clear buffer and update time after sending
                            streaming_buffer = ""
                            last_chunk_time = current_time
                    else:
                        accumulated_content += delta

                elif prefix == "ad":
                    finish_data = json.loads(content)
                    finish_reason = finish_data.get("finishReason", "stop")

            except (ValueError, json.JSONDecodeError):
                logging.warning(f"STREAMER [ID: {request_id}]: Could not parse data: {raw_data}")
                continue

            # Yield control to event loop after processing
            if is_streaming and model_type == "chat":
                await asyncio.sleep(0.001)  # Small delay to help with network flush
            else:
                await asyncio.sleep(0)

        # --- Final Response Generation ---

        # Flush any remaining buffer content for streaming chat
        if is_streaming and model_type == "chat" and streaming_buffer:
            chunk = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "content": streaming_buffer
                    },
                    "finish_reason": None
                }],
                "system_fingerprint": f"fp_{uuid.uuid4().hex[:8]}"
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            accumulated_content += streaming_buffer
            streaming_buffer = ""

        if model_type in ["image", "video"]:
            logging.info(f"MEDIA [ID: {request_id}]: Found {len(media_urls)} media file(s). Returning URLs directly.")
            # Format the URLs based on their type
            if model_type == "video":
                accumulated_content = "\n".join(media_urls)  # Return raw URLs for videos
            else:  # Default to image handling
                accumulated_content = "\n".join([f"![Generated Image]({url})" for url in media_urls])

        if is_streaming:
            if model_type in ["image", "video"]:
                chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": accumulated_content
                        },
                        "finish_reason": finish_reason or "stop"
                    }],
                    "system_fingerprint": f"fp_{uuid.uuid4().hex[:8]}"
                }
                yield f"data: {json.dumps(chunk)}\n\n"

            # Send final chunk with finish_reason for chat models
            if model_type == "chat":
                final_chunk = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": finish_reason or "stop"
                    }],
                    "system_fingerprint": f"fp_{uuid.uuid4().hex[:8]}"
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"

            # Send [DONE] immediately
            yield "data: [DONE]\n\n"
        else:
            # For non-streaming, send the complete JSON object with the URL content
            complete_response = {
                "id": response_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": accumulated_content
                    },
                    "finish_reason": finish_reason or "stop"
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                },
                "system_fingerprint": f"fp_{uuid.uuid4().hex[:8]}"
            }
            yield json.dumps(complete_response)

        # 记录请求成功
        input_tokens = estimateTokens(str(persistent_req.openai_request if persistent_req else {}))
        output_tokens = estimateTokens(accumulated_content)
        log_request_end(request_id, True, input_tokens, output_tokens, response_content=accumulated_content)
        
        # 广播到监控客户端
        await broadcast_to_monitors({
            "type": "request_end",
            "request_id": request_id,
            "success": True,
            "duration": time.time() - start_time
        })

    except asyncio.CancelledError:
        logging.warning(f"GENERATOR [ID: {request_id}]: Client disconnected.")

        # Send abort message to browser if WebSocket is connected
        if browser_ws:
            try:
                await browser_ws.send_text(json.dumps({
                    "type": "abort_request",
                    "request_id": request_id
                }))
                logging.info(f"GENERATOR [ID: {request_id}]: Sent abort message to browser")
            except Exception as e:
                logging.error(f"GENERATOR [ID: {request_id}]: Failed to send abort message: {e}")

        # Re-raise to properly handle the cancellation
        raise

    except KeyboardInterrupt:
        logging.info(f"GENERATOR [ID: {request_id}]: Keyboard interrupt received, cleaning up...")
        raise
    except Exception as e:
        logging.error(f"GENERATOR [ID: {request_id}]: Error: {e}", exc_info=True)
        
        # 记录错误
        log_error(request_id, type(e).__name__, str(e), traceback.format_exc())
        log_request_end(request_id, False, 0, 0, str(e))
        
        # 广播错误到监控客户端
        await broadcast_to_monitors({
            "type": "request_error",
            "request_id": request_id,
            "error": str(e),
            "timestamp": time.time()
        })
        
    finally:
        # Clean up both tracking systems
        if request_id in response_channels:
            del response_channels[request_id]
            logging.info(f"GENERATOR [ID: {request_id}]: Cleaned up response channel.")

        # Mark request as completed in persistent manager
        request_manager.complete_request(request_id)


def create_lmarena_request_body(openai_req: dict) -> (dict, list):
    model_name = openai_req["model"]

    if model_name not in MODEL_REGISTRY:
        raise ValueError(f"Model '{model_name}' not found in registry. Available models: {list(MODEL_REGISTRY.keys())}")

    model_info = MODEL_REGISTRY[model_name]
    model_id = model_info.get("id", model_name)
    modality = model_info.get("type", "chat")
    evaluation_id = str(uuid.uuid4())

    files_to_upload = []
    processed_messages = []

    # Process messages to extract files and clean content
    for msg in openai_req['messages']:
        content = msg.get("content", "")
        new_msg = msg.copy()

        if isinstance(content, list):
            # Handle official multimodal content array
            text_parts = []
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    image_url = part.get("image_url", {}).get("url", "")
                    match = re.match(r"data:(image/\w+);base64,(.*)", image_url)
                    if match:
                        mime_type, base64_data = match.groups()
                        file_ext = mime_type.split('/')
                        filename = f"upload-{uuid.uuid4()}.{file_ext}"
                        files_to_upload.append({"fileName": filename, "contentType": mime_type, "data": base64_data})
            new_msg["content"] = "\n".join(text_parts)
            processed_messages.append(new_msg)

        elif isinstance(content, str):
            # Handle simple string content that might contain data URLs
            text_content = content
            matches = re.findall(r"data:(image/\w+);base64,([a-zA-Z0-9+/=]+)", content)
            if matches:
                logging.info(f"Found {len(matches)} data URL(s) in string content.")
                for mime_type, base64_data in matches:
                    file_ext = mime_type.split('/')
                    filename = f"upload-{uuid.uuid4()}.{file_ext}"
                    files_to_upload.append({"fileName": filename, "contentType": mime_type, "data": base64_data})
                # Remove all found data URLs from the text to be sent
                text_content = re.sub(r"data:image/\w+;base64,[a-zA-Z0-9+/=]+", "", text_content).strip()

            new_msg["content"] = text_content
            processed_messages.append(new_msg)

        else:
            # If content is not a list or string, just pass it through
            processed_messages.append(msg)

    # Find the last user message index
    last_user_message_index = -1
    for i in range(len(processed_messages) - 1, -1, -1):
        if processed_messages[i].get("role") == "user":
            last_user_message_index = i
            break

    # Insert empty user message after the last user message (only for chat models)
    if modality == "chat" and last_user_message_index != -1:
        # Insert empty user message after the last user message
        insert_index = last_user_message_index + 1
        empty_user_message = {"role": "user", "content": " "}
        processed_messages.insert(insert_index, empty_user_message)
        logging.info(
            f"Added empty user message after last user message at index {last_user_message_index} for chat model")

    # Build Arena-formatted messages
    arena_messages = []
    message_ids = [str(uuid.uuid4()) for _ in processed_messages]
    for i, msg in enumerate(processed_messages):
        parent_message_ids = [message_ids[i - 1]] if i > 0 else []

        original_role = msg.get("role")
        role = "user" if original_role not in ["user", "assistant", "data"] else original_role

        arena_messages.append({
            "id": message_ids[i], "role": role, "content": msg['content'],
            "experimental_attachments": [], "parentMessageIds": parent_message_ids,
            "participantPosition": "a", "modelId": model_id if role == 'assistant' else None,
            "evaluationSessionId": evaluation_id, "status": "pending", "failureReason": None,
        })

    user_message_id = message_ids[-1] if message_ids else str(uuid.uuid4())
    model_a_message_id = str(uuid.uuid4())
    arena_messages.append({
        "id": model_a_message_id, "role": "assistant", "content": "",
        "experimental_attachments": [], "parentMessageIds": [user_message_id],
        "participantPosition": "a", "modelId": model_id,
        "evaluationSessionId": evaluation_id, "status": "pending", "failureReason": None,
    })

    payload = {
        "id": evaluation_id, "mode": "direct", "modelAId": model_id,
        "userMessageId": user_message_id, "modelAMessageId": model_a_message_id,
        "messages": arena_messages, "modality": modality,
    }

    return payload, files_to_upload


@app.get("/v1/models")
async def get_models():
    """Lists all available models in an OpenAI-compatible format."""
    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": int(asyncio.get_event_loop().time()),
                "owned_by": "lmarena",
                "type": model_info.get("type", "chat")
            }
            for model_name, model_info in MODEL_REGISTRY.items()
        ],
    }


@app.post("/v1/refresh-models")
async def refresh_models():
    """Request model registry refresh from browser script."""
    if browser_ws:
        try:
            # Send refresh request to browser
            await browser_ws.send_text(json.dumps({
                "type": "refresh_models"
            }))

            return {
                "success": True,
                "message": "Model refresh request sent to browser",
                "models": list(MODEL_REGISTRY.keys())
            }
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logging.error(f"Failed to send refresh request: {e}")
            return {
                "success": False,
                "message": "Failed to send refresh request to browser",
                "models": list(MODEL_REGISTRY.keys())
            }
    else:
        return {
            "success": False,
            "message": "No browser connection available",
            "models": list(MODEL_REGISTRY.keys())
        }

# --- Prometheus Metrics Endpoint ---
@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# --- 监控相关API端点 ---
@app.get("/api/stats/summary")
async def get_stats_summary():
    """获取统计摘要"""
    # 从日志文件计算统计
    recent_logs = log_manager.read_request_logs(limit=10000)  # 读取最近的日志
    
    # 计算24小时内的统计
    current_time = time.time()
    day_ago = current_time - 86400
    
    recent_24h_logs = [log for log in recent_logs if log.get('timestamp', 0) > day_ago]
    
    total_requests = len(recent_24h_logs)
    successful = sum(1 for log in recent_24h_logs if log.get('status') == 'success')
    failed = total_requests - successful
    
    total_input_tokens = sum(log.get('input_tokens', 0) for log in recent_24h_logs)
    total_output_tokens = sum(log.get('output_tokens', 0) for log in recent_24h_logs)
    
    durations = [log.get('duration', 0) for log in recent_24h_logs if log.get('duration', 0) > 0]
    avg_duration = sum(durations) / len(durations) if durations else 0
    
    # 获取性能统计
    perf_stats = performance_monitor.get_stats()
    model_perf = performance_monitor.get_model_stats()
    
    # 构建模型统计
    model_stats = []
    for model_name, usage in realtime_stats.model_usage.items():
        perf = model_perf.get(model_name, {})
        model_stats.append({
            "model": model_name,
            "total_requests": usage['requests'],
            "successful_requests": usage['requests'] - usage['errors'],
            "failed_requests": usage['errors'],
            "total_input_tokens": usage.get('tokens', 0) // 2,  # 粗略估算
            "total_output_tokens": usage.get('tokens', 0) // 2,
            "avg_duration": perf.get('avg_response_time', 0),
            "qps": perf.get('qps', 0),
            "error_rate": perf.get('error_rate', 0)
        })
    
    return {
        "summary": {
            "total_requests": total_requests,
            "successful": successful,
            "failed": failed,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "avg_duration": avg_duration,
            "success_rate": (successful / total_requests * 100) if total_requests > 0 else 0
        },
        "performance": perf_stats,
        "model_stats": sorted(model_stats, key=lambda x: x['total_requests'], reverse=True),
        "active_requests": len(realtime_stats.active_requests),
        "browser_connected": browser_ws is not None,
        "monitor_clients": len(monitor_clients),
        "uptime": time.time() - startup_time
    }

@app.get("/api/logs/requests")
async def get_request_logs(limit: int = 100, offset: int = 0, model: str = None):
    """获取请求日志"""
    logs = log_manager.read_request_logs(limit, offset, model)
    return logs

@app.get("/api/logs/errors")
async def get_error_logs(limit: int = 50):
    """获取错误日志"""
    logs = log_manager.read_error_logs(limit)
    return logs

@app.get("/api/logs/download")
async def download_logs(log_type: str = "requests"):
    """下载日志文件"""
    if log_type == "requests":
        file_path = log_manager.request_log_path
        filename = "requests.jsonl"
    elif log_type == "errors":
        file_path = log_manager.error_log_path
        filename = "errors.jsonl"
    else:
        raise HTTPException(status_code=400, detail="Invalid log type")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    
    return StreamingResponse(
        open(file_path, 'rb'),
        media_type="application/x-jsonlines",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/api/request/{request_id}")
async def get_request_details(request_id: str):
    """获取请求详情"""
    details = request_details_storage.get(request_id)
    if not details:
        raise HTTPException(status_code=404, detail="Request details not found")
    
    return {
        "request_id": details.request_id,
        "timestamp": details.timestamp,
        "model": details.model,
        "status": details.status,
        "duration": details.duration,
        "input_tokens": details.input_tokens,
        "output_tokens": details.output_tokens,
        "error": details.error,
        "request_params": details.request_params,
        "request_messages": details.request_messages,
        "response_content": details.response_content,
        "headers": details.headers
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "browser_connected": browser_ws is not None,
        "active_requests": len(request_manager.active_requests),
        "uptime": time.time() - startup_time,
        "models_loaded": len(MODEL_REGISTRY),
        "monitor_clients": len(monitor_clients),
        "log_files": {
            "requests": str(log_manager.request_log_path),
            "errors": str(log_manager.error_log_path)
        }
    }



# --- 配置管理API ---
@app.get("/api/config")
async def get_config():
    """获取当前配置"""
    return config_manager.dynamic_config


@app.post("/api/config")
async def update_config(request: Request):
    """更新配置"""
    try:
        config_data = await request.json()

        # 更新配置
        config_manager._deep_merge(config_manager.dynamic_config, config_data)
        config_manager.save_config()

        # 应用某些配置的即时更改
        if 'request' in config_data:
            if 'timeout_seconds' in config_data['request']:
                Config.REQUEST_TIMEOUT_SECONDS = config_data['request']['timeout_seconds']
            if 'max_concurrent_requests' in config_data['request']:
                Config.MAX_CONCURRENT_REQUESTS = config_data['request']['max_concurrent_requests']

        return {"status": "success", "message": "配置已更新"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/config/quick-links")
async def update_quick_links(request: Request):
    """更新快速链接"""
    try:
        links = await request.json()
        config_manager.set('quick_links', links)
        return {"status": "success", "message": "快速链接已更新"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/system/info")
async def get_system_info():
    """获取系统信息"""
    display_ip = config_manager.get_display_ip()
    port = config_manager.get('network.port', Config.PORT)

    return {
        "server_urls": {
            "local": f"http://localhost:{port}",
            "network": f"http://{display_ip}:{port}",
            "monitor": f"http://{display_ip}:{port}/monitor",
            "metrics": f"http://{display_ip}:{port}/metrics",
            "health": f"http://{display_ip}:{port}/api/health/detailed"
        },
        "detected_ips": get_all_local_ips(),
        "current_ip": display_ip,
        "auto_detect": config_manager.get('network.auto_detect_ip', True)
    }


def get_all_local_ips():
    """获取所有本地IP地址"""
    import socket
    ips = []
    try:
        hostname = socket.gethostname()
        all_ips = socket.gethostbyname_ex(hostname)[2]
        for ip in all_ips:
            if not ip.startswith('127.') and not ip.startswith('198.18.'):
                ips.append(ip)
    except:
        pass
    return ips


@app.get("/api/health/detailed")
async def get_detailed_health():
    """获取简化的健康状态"""
    # 计算基本指标
    active_count = len(realtime_stats.active_requests)
    uptime_seconds = time.time() - startup_time
    
    # 简单的性能统计
    perf_stats = performance_monitor.get_stats() if 'performance_monitor' in globals() else {}
    
    return {
        "status": "healthy" if browser_ws else "disconnected",
        "browser_connected": browser_ws is not None,
        "active_requests": active_count,
        "max_concurrent_requests": Config.MAX_CONCURRENT_REQUESTS,
        "capacity_usage_percent": (active_count / Config.MAX_CONCURRENT_REQUESTS * 100),
        "models_loaded": len(MODEL_REGISTRY),
        "monitor_clients": len(monitor_clients),
        "uptime": {
            "seconds": uptime_seconds,
            "hours": uptime_seconds / 3600,
            "days": uptime_seconds / 86400
        },
        "performance": {
            "avg_response_time": perf_stats.get('avg_response_time', 0)
        },
        "log_files": {
            "requests": str(log_manager.request_log_path),
            "errors": str(log_manager.error_log_path)
        }
    }


# --- 监控面板HTML (优化版) ---
@app.get("/monitor", response_class=HTMLResponse)
async def monitor_dashboard():
    """返回监控面板HTML页面"""
    # 读取外部HTML文件
    html_file_path = Path(__file__).parent / "index.html"
    
    # 检查文件是否存在
    if not html_file_path.exists():
        return HTMLResponse(
            content="<h1>监控面板文件未找到</h1><p>请确保 index.html 文件在正确的位置。</p>",
            status_code=404
        )
    
    # 读取并返回HTML内容
    with open(html_file_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    
    return HTMLResponse(content=html_content)

print("\n" + "="*60)
print("🚀 LMArena 反向代理服务器")
print("="*60)
print(f"📍 本地访问: http://localhost:{Config.PORT}")
print(f"📍 局域网访问: http://{get_local_ip()}:{Config.PORT}")
print(f"📊 监控面板: http://{get_local_ip()}:{Config.PORT}/monitor")
print("="*60)
print("💡 提示: 请确保浏览器扩展已安装并启用")
print("💡 如果使用代理软件，局域网IP可能不准确")
print("💡 如果局域网IP不准确可以在此文件中修改，将MANUAL_IP = None  修改为MANUAL_IP = 你指定的IP地址如：192.168.0.1")
print("="*60 + "\n")
if __name__ == "__main__":
    uvicorn.run(app, host=Config.HOST, port=Config.PORT)
