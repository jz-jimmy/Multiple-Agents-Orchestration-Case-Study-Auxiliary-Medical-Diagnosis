import websocket
import json
import base64
import hashlib
import hmac
from urllib.parse import urlencode
import time
import ssl
from datetime import datetime, timezone
import threading
import pyaudio
import queue
import logging
import requests  
import asyncio
import websockets
import threading
import os
import urllib
from collections import deque

lfasr_host = 'https://raasr.xfyun.cn/v2/api'
# 请求的接口名
api_upload = '/upload'
api_get_result = '/getResult'
# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

STATUS_FIRST_FRAME = 0
STATUS_CONTINUE_FRAME = 1
STATUS_LAST_FRAME = 2


def call_deepseek_with_retry(payload, api_key, max_retries=3, backoff=3):
    """
    调用 DeepSeek API，自动重试机制。
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    for attempt in range(1, max_retries + 1):
        try:
            logger.info("调用DeepSeek API生成结构化病历...（第 %d 次尝试）", attempt)
            response = requests.post("https://api.deepseek.com/v1/chat/completions", json=payload, headers=headers, timeout=60)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.warning("第 %d 次请求失败：%s", attempt, str(e))
            if attempt < max_retries:
                time.sleep(backoff * attempt)
            else:
                logger.error("病历生成失败: %s", str(e))
                return {"choices": [{"message": {"content": "生成失败"}}]}

# ZJY
def call_fay_agent(question: str, timeout: int = 30) -> str:
    """
    Minimal wrapper that forwards the user's question to Fay
    and returns the answer as plain text.

    For an MVP you can:
    1.   POST to your local MCP server that already proxies Fay, e.g.
         r = requests.post("http://localhost:8000/chat", json={"message": question})
    2.   Or, if Fay is running inside the same Python process,
         import the agent and call `agent.chat(question)`.
    """
    # --- Stub implementation ---
    try:
        r = requests.post("http://localhost:8000/chat",
                          json={"message": question, "username": "User"},
                          timeout=timeout)
        r.raise_for_status()
        return r.json().get("answer", "（无回答）")
    except Exception as e:
        logger.error(f"Fay chat API error: {e}")
        return "抱歉，暂时无法回答。"

# ZJY


class WebSocketServer:
    def __init__(self, host='0.0.0.0', port=8765):
        self.host = host
        self.port = port
        self.clients = set()
        self.server = None  
        self.asr_instance = None
        self.loop = None
        self.server_thread = None
    
    def start(self, asr_instance):
        self.asr_instance = asr_instance
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        self.server_thread = threading.Thread(target=self._run_server)
        self.server_thread.daemon = True
        self.server_thread.start()
    
    def _run_server(self):
        # 修复：添加 path 参数
        async def handler(websocket):
            websocket.max_size = 16 * 1024 * 1024  # 10MB
            self.clients.add(websocket)
            try:
                # 发送初始状态
                await websocket.send(json.dumps({
                    "type": "status",
                    "data": "等待开始录音"
                }))
                
                # 保持连接
                async for message in websocket:
                    data = json.loads(message)
                    
                    if data.get("command") == "start":
                        self.asr_instance.start()
                        await self.broadcast({
                            "type": "status",
                            "data": "录音中..."
                        })
                    elif data.get("command") == "stop":
                        self.asr_instance.stop()
                        await self.broadcast({
                            "type": "status",
                            "data": "录音已停止"
                        })
                    elif data.get("command") == "generate_emr":
                        if data.get("source") == "text":
                            text = data.get("text", "")
                            
                            def generate_from_text_task():
                                try:
                                    emr_data = self.asr_instance.emr_generator.generate_from_text(text)
                                    asyncio.run_coroutine_threadsafe(
                                        self.broadcast({
                                            "type": "emr",
                                            "data": emr_data
                                        }), 
                                        self.loop
                                    )
                                except Exception as e:
                                    logger.error(f"从文本生成病历出错: {str(e)}")
                            
                            threading.Thread(target=generate_from_text_task, daemon=True).start()
                        else:
                            # 使用线程池执行耗时操作
                            def generate_emr_task():
                                try:
                                    emr_data = self.asr_instance.generate_emr()
                                    asyncio.run_coroutine_threadsafe(
                                        self.broadcast({
                                            "type": "emr",
                                            "data": emr_data
                                        }), 
                                        self.loop
                                    )
                                except Exception as e:
                                    logger.error(f"生成病历出错: {str(e)}")
                            
                            threading.Thread(target=generate_emr_task, daemon=True).start()
                        
                    elif data.get("command") == "process_audio":
                        # 将文件处理放到独立线程中
                        def process_audio_task():
                            try:
                                print(f"收到文件处理请求，文件名: {data.get('filename')}")
                                
                                if not data.get("data"):
                                    print("错误: 未接收到文件数据")
                                    asyncio.run_coroutine_threadsafe(
                                        websocket.send(json.dumps({
                                            "type": "error",
                                            "message": "未接收到文件数据"
                                        })), 
                                        self.loop
                                    )
                                    return
                                    
                                temp_dir = "temp_audio"
                                os.makedirs(temp_dir, exist_ok=True)
                                
                                file_ext = os.path.splitext(data.get("filename", "audio"))[1] or ".mp3"
                                temp_file_path = os.path.join(temp_dir, f"upload_{int(time.time())}{file_ext}")
                                
                                print(f"正在保存临时文件到: {temp_file_path}")
                                with open(temp_file_path, "wb") as f:
                                    f.write(base64.b64decode(data["data"]))
                                
                                print(f"临时文件已保存，大小: {os.path.getsize(temp_file_path)} 字节")
                                
                                # 通知前端文件已接收
                                asyncio.run_coroutine_threadsafe(
                                    websocket.send(json.dumps({
                                        "type": "status",
                                        "data": "正在处理音频文件..."
                                    })), 
                                    self.loop
                                )
                                
                                print("正在初始化XunfeiFileASR...")
                                file_asr = XunfeiFileASR(
                                    self.asr_instance.app_id,
                                    self.asr_instance.secret_key,
                                    temp_file_path
                                )
                                
                                print("正在调用讯飞API...")
                                result = file_asr.get_result()
                                print("讯飞API返回结果:", result)
                                # 保存转写结果
                                with open(self.asr_instance.transcript_file, "w", encoding="utf-8") as f:
                                    f.write(result)
                                
                                # 返回转写结果
                                asyncio.run_coroutine_threadsafe(
                                    websocket.send(json.dumps({
                                        "type": "file_transcript",
                                        "data": result
                                    })), 
                                    self.loop
                                )
                                
                                # 生成病历
                                print("正在生成病历...")
                                emr_data = self.asr_instance.generate_emr()
                                asyncio.run_coroutine_threadsafe(
                                    websocket.send(json.dumps({
                                        "type": "file_emr",
                                        "data": emr_data
                                    })), 
                                    self.loop
                                )
                                
                                print("文件处理流程完成")
                                
                            except Exception as e:
                                print(f"文件处理出错: {str(e)}")
                                asyncio.run_coroutine_threadsafe(
                                    websocket.send(json.dumps({
                                        "type": "error",
                                        "message": f"文件处理失败: {str(e)}"
                                    })), 
                                    self.loop
                                )
                            finally:
                                # 清理临时文件
                                if 'temp_file_path' in locals() and os.path.exists(temp_file_path):
                                    try:
                                        os.remove(temp_file_path)
                                        print(f"已删除临时文件: {temp_file_path}")
                                    except Exception as e:
                                        print(f"删除临时文件失败: {str(e)}")
                        
                        # 启动文件处理线程
                        threading.Thread(target=process_audio_task, daemon=True).start()
                    
                    elif data.get("command") == "process_document":
                        def process_document_task():
                            try:
                                # 1. 保存上传的文件
                                file_data = base64.b64decode(data["data"])
                                file_ext = os.path.splitext(data.get("filename", "doc"))[1] or ".jpg"
                                temp_path = os.path.join("temp_upload", f"doc_{int(time.time())}{file_ext}")
                                
                                os.makedirs("temp_upload", exist_ok=True)
                                with open(temp_path, "wb") as f:
                                    f.write(file_data)
                                
                                # 2. 初始化OCR识别器
                                doc_ocr = XunfeiDocOCR(
                                    self.asr_instance.app_id,
                                    self.asr_instance.api_key,
                                    self.asr_instance.api_secret,
                                    self.asr_instance.deepseek_api_key  # 新增：传入DeepSeek API Key
                                )
                                
                                # 3. 识别并生成病历
                                emr_data = doc_ocr.generate_emr(temp_path)
                                
                                # 4. 返回结果给前端
                                asyncio.run_coroutine_threadsafe(
                                    websocket.send(json.dumps({
                                        "type": "document_result",
                                        "data": emr_data  # 直接返回结构化病历
                                    })),
                                    self.loop
                                )
                                
                            except Exception as e:
                                logger.error(f"文档处理失败: {str(e)}")
                                asyncio.run_coroutine_threadsafe(
                                    websocket.send(json.dumps({
                                        "type": "error",
                                        "message": f"文档处理失败: {str(e)}"
                                    })),
                                    self.loop
                                )
                            finally:
                                if 'temp_path' in locals() and os.path.exists(temp_path):
                                    os.remove(temp_path)
                        
                        threading.Thread(target=process_document_task, daemon=True).start()

                    elif data.get("command") == "fay_chat":
                        question = data.get("question", "")
                        # Run the call in a worker thread so we never block the event‑loop
                        def fay_task():
                            try:
                                answer = call_fay_agent(question)   # see helper below
                                asyncio.run_coroutine_threadsafe(
                                    self.broadcast(
                                        {"type": "fay_chat_response",
                                        "question": question,
                                        "answer":  answer}),
                                    self.loop)
                            except Exception as e:
                                logger.error(f"Fay chat error: {e}")
                        threading.Thread(target=fay_task, daemon=True).start()


            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                self.clients.remove(websocket)
        
        async def broadcast_transcript():
            while True:
                try:
                    with open("transcript.txt", "r", encoding="utf-8") as f:
                        transcript_content = f.read()
                        await self.broadcast({
                            "type": "transcript",
                            "data": transcript_content
                        })
                except Exception as e:
                    logger.warning(f"读取转录文件失败: {str(e)}")
                await asyncio.sleep(2)  # 每2秒检查一次更新
        
        async def main():
            self.server = await websockets.serve(handler, self.host, self.port, max_size=16 * 1024 * 1024)
            asyncio.create_task(broadcast_transcript())
            await self.server.wait_closed()
        
        self.loop.run_until_complete(main())
    
    async def broadcast(self, message):
        if not self.clients:
            return
            
        message_str = json.dumps(message)
        for client in self.clients.copy():
            try:
                await client.send(message_str)
            except:
                self.clients.remove(client)

    async def handle_medical_report(self, data):
        try:
            # 1. 保存临时文件
            file_data = base64.b64decode(data["data"])
            temp_path = f"temp_upload/{int(time.time())}.jpg"
            with open(temp_path, "wb") as f:
                f.write(file_data)
            
            # 2. 调用OCR服务
            ocr = XunfeiDocOCR(self.app_id, self.api_key, self.api_secret)
            structured_data = ocr.recognize_document(temp_path)
            
            # 4. 返回结果
            await self.send({
                "type": "medical_report_result",
                "data": structured_data  # 结构化数据而非原始内容
            })
            
        except Exception as e:
            logger.error(f"报告处理失败: {str(e)}")
            await self.send({
                "type": "error",
                "message": f"报告解析失败: {str(e)}",
                "raw_data": None
            })
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    
    def stop(self):
        if self.server:
            self.server.close()
            self.asr_instance.stop()
            asyncio.run_coroutine_threadsafe(self.server.wait_closed(), self.loop)
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

# ✅ 新增病历生成器类
class MedicalEMRGenerator:
    def __init__(self, api_key):
        self.api_key = api_key
        self.api_url = "https://api.deepseek.com/v1/chat/completions"
        self.dialogue_cache = []
        self.lock = threading.Lock()
        self.max_history = 30  # 最多保留 30 句对话上下文

        # 系统提示词
        self.system_prompt = """
你是一个专业的医疗助手，请从病人与医生之间的真实对话内容中，尽可能全面、准确地提取结构化病历信息。

请只输出纯文本格式的结构化信息，不要使用任何标记符号，如**加粗**、HTML标签或Markdown语法。

请严格基于对话内容推理，不要主观臆断或编造。如果某项确实没有明确信息，请填写"无相关信息"。不要求病历用词工整，尽可能贴近对话原意。

【输出格式示例，仅供参考，不要直接抄】：
主诉：患者反复胸痛两天，伴随咳嗽和呼吸急促。
现病史：症状起于三天前受凉之后，初为咽干咳嗽，逐渐发展为胸痛和气促，夜间加重，无明显发热。
既往史：有多年吸烟史，无糖尿病、心脏病。
体征：呼吸急促，听诊右肺有湿罗音。
实验室检查结果：暂未提及。
医生建议：建议胸片检查，进一步明确感染或肺炎可能，必要时考虑抗生素治疗。

请严格使用以下输出格式：

主诉：
现病史：
既往史：
体征：
实验室检查结果：
医生建议：
"""

    def generate_from_transcript(self, transcript_file):
        """从转录文件生成结构化病历"""
        try:
            with open(transcript_file, "r", encoding="utf-8") as f:
                transcript_content = f.read()
                
            # 构建请求
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": self.system_prompt.strip()},
                    {"role": "user", "content": f"对话内容如下：\n{transcript_content}"}
                ],
                "temperature": 0.3
            }
            
            logger.info("调用DeepSeek API生成结构化病历...")
            response_data = call_deepseek_with_retry(payload, self.api_key)
            result_text = response_data["choices"][0]["message"]["content"]
            logger.info(f"📄 DeepSeek 返回原始内容：\n{result_text}")

            # 解析并保存结果
            parsed = self.parse_output(result_text)
            with open("medical_record.json", "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=4)
            logger.info("📝 JSON 保存成功")
            
            return parsed
            
        except Exception as e:
            logger.error(f"病历生成失败: {str(e)}")
            return None
        
    def generate_from_image(self, ocr_text):
        """从OCR识别文本生成结构化病历"""
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            # 修改系统提示词适配图像输入
            image_prompt = self.system_prompt + """
            \n当前输入来自医疗报告图片识别结果，请特别注意：
            1. 数值单位可能识别不完整（如把g/L识别成9L）
            2. 表格数据可能错位
            3. 注意区分报告中的检查项目名称和结果值
            """
            
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": image_prompt.strip()},
                    {"role": "user", "content": f"报告识别内容：\n{ocr_text}"}
                ],
                "temperature": 0.1  # 降低随机性保证数据准确性
            }
            
            response_data = call_deepseek_with_retry(payload, self.api_key)
            result_text = response_data["choices"][0]["message"]["content"]
                        # 解析并保存结果
            parsed = self.parse_output(result_text)
            with open("medical_record.json", "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=4)
            logger.info("📝 JSON 保存成功")
            return parsed
            
        except Exception as e:
            logger.error(f"图像病历生成失败: {str(e)}")
            return None
    
    def parse_output(self, text):
        fields = ["主诉", "现病史", "既往史", "体征", "实验室检查结果", "医生建议"]
        result = {}
        for field in fields:
            start = text.find(field + "：")
            if start == -1:
                result[field] = "未识别"
                logger.warning(f"⚠️ 无法识别字段：{field}")
                continue
                       # 查找下一个字段的开始位置
            end = len(text)
            for other_field in fields:
                if other_field == field:
                    continue
                pos = text.find(other_field + "：", start + len(field) + 1)
                if 0 < pos < end:
                    end = pos
            
            result[field] = text[start + len(field) + 1:end].strip()
        return result

class XunfeiStreamASR:
    def __init__(self, app_id, api_key, api_secret,secret_key, deepseek_api_key):
        self.app_id = app_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.secret_key=secret_key
        self.deepseek_api_key=deepseek_api_key
        self.emr_generator = MedicalEMRGenerator(deepseek_api_key)
        self.ws_server = WebSocketServer(port=8765)
        # 业务参数
        self.business_args = {
            "domain": "iat",
            "language": "zh_cn",
            "accent": "mandarin",
            "vad_eos": 20000
        }
        
        # 状态变量
        self.is_running = False
        self.ws_connected = False
        self.session_start_time = 0
        self.reconnecting = False
        self.auto_reconnect = True
        self.last_reconnect_time = 0
        
        # 音频参数
        self.chunk_size = 1280  # 每帧音频大小(40ms)
        self.sample_rate = 16000  # 采样率
        
        # 音频缓存配置
        self.cache_enabled = True
        self.cache_duration = 5  # 缓存5秒音频
        self.cache_size = int(self.sample_rate * self.cache_duration / (self.chunk_size / 2))
        self.audio_cache = deque(maxlen=self.cache_size)
        self.cache_lock = threading.Lock()
        
        # 音频设备
        self.audio = pyaudio.PyAudio()
        self.stream = None
        
        # 结果处理
        self.result_queue = queue.Queue()
        self.lock = threading.Lock()

        # 中间结果保存文件
        self.transcript_file = "transcript.txt"
        with open(self.transcript_file, "w", encoding="utf-8") as f:
            f.write("")
        with open("medical_record.json", "w", encoding="utf-8") as f:
            json.dump({}, f)  # 写入空的JSON对象
    
    
    def _append_to_transcript(self, text):
        """将文本追加到转录文件中"""
        try:
            with open(self.transcript_file, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {text}\n")
        except Exception as e:
            logger.error(f"写入转录文件失败: {str(e)}")

    def _generate_url(self):
        """生成WebSocket连接URL"""
        url = "wss://ws-api.xfyun.cn/v2/iat"
        now = datetime.now(timezone.utc)
        date = now.strftime('%a, %d %b %Y %H:%M:%S GMT')
        signature_origin = f"host: ws-api.xfyun.cn\ndate: {date}\nGET /v2/iat HTTP/1.1"
        
        signature_sha = hmac.new(
            self.api_secret.encode('utf-8'), 
            signature_origin.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        
        signature_sha = base64.b64encode(signature_sha).decode('utf-8')
        authorization_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature_sha}"'
        )
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode('utf-8')
        
        params = {
            "authorization": authorization,
            "date": date,
            "host": "ws-api.xfyun.cn"
        }
        
        return f"{url}?{urlencode(params)}"
    
    def _on_open(self, ws):
        """WebSocket连接打开回调"""
        with self.lock:
            logger.info("WebSocket连接已建立")
            self.ws_connected = True
            self.reconnecting = False
            self.first_frame_sent = False
            
            # 启动音频发送线程
            self.send_thread = threading.Thread(target=self._send_audio_data, daemon=True)
            self.send_thread.start()

    def _send_audio_data(self):
        """发送音频数据"""
        status = STATUS_FIRST_FRAME
        self.session_start_time = time.time()
        last_data_time = time.time()
        
        while self.is_running and self.ws_connected:
            try:
                # 检查会话是否超50秒
                if time.time() - self.session_start_time > 50:
                    logger.info("会话已超50秒，主动重新连接...")
                    self._reconnect()
                    return  # 退出当前发送线程
                    
                # 发送保活帧防止断开（每9.5秒）
                if time.time() - last_data_time > 9.5:
                    logger.debug("发送保活帧")
                    frame = {
                        "data": {
                            "status": STATUS_CONTINUE_FRAME,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(b'\x00' * self.chunk_size).decode('utf-8')
                        }
                    }
                    self.ws.send(json.dumps(frame))
                    last_data_time = time.time()
                
                # 从队列获取音频数据
                audio_data = self.audio_queue.get(timeout=0.1)
                
                # 构建并发送音频帧
                if not self.first_frame_sent:
                    # 第一帧包含common和business
                    frame = {
                        "common": {"app_id": self.app_id},
                        "business": self.business_args,
                        "data": {
                            "status": status,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(audio_data).decode('utf-8')
                        }
                    }
                    self.first_frame_sent = True
                else:
                    frame = {
                        "data": {
                            "status": status,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(audio_data).decode('utf-8')
                        }
                    }
                
                self.ws.send(json.dumps(frame))
                last_data_time = time.time()
                
                # 第一帧后切换状态
                if status == STATUS_FIRST_FRAME:
                    status = STATUS_CONTINUE_FRAME
                    
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"发送音频出错: {str(e)}")
                if "Connection is already closed" in str(e):
                    # 如果连接已关闭，尝试重连
                    self._reconnect()
                break
        
        # 发送结束帧
        if self.ws_connected:
            try:
                frame = {
                    "data": {
                        "status": STATUS_LAST_FRAME,
                        "format": "audio/L16;rate=16000",
                        "encoding": "raw",
                        "audio": ""
                    }
                }
                self.ws.send(json.dumps(frame))
                logger.info("已发送结束帧")
            except Exception as e:
                logger.error(f"发送结束帧出错: {str(e)}")
    
    def on_message(self, ws, message):
        """处理WebSocket消息"""
        try:
            data = json.loads(message)
            code = data.get("code", 0)
            
            if code != 0:
                error_msg = data.get("message", "未知错误")
                logger.error(f"识别错误: {error_msg} (code: {code})")
                return
                
            # 提取并处理结果
            result_data = data.get("data", {})
            if "result" in result_data:
                ws_list = result_data["result"].get("ws", [])
                result_text = ""
                
                for item in ws_list:
                    for word in item.get("cw", []):
                        result_text += word.get("w", "")
                
                # 只处理中间结果
                if not (result_data.get("status") == 2 or result_data.get("result", {}).get("ls", False)):
                    logger.info(f"中间结果: {result_text}")
                    # 将中间结果放入队列
                    self.result_queue.put(result_text)
                    self._append_to_transcript(result_text)
                    
        except Exception as e:
            logger.error(f"处理消息出错: {str(e)}")
    
    def _on_error(self, ws, error):
        """WebSocket错误回调"""
        logger.error(f"WebSocket错误: {str(error)}")
        self.ws_connected = False
        if self.auto_reconnect and self.is_running:
            self._reconnect()
    
    def _reconnect(self):
        """重新连接WebSocket - 最终优化版本"""
        with self.lock:  # 添加锁保证线程安全
            if not self.is_running or self.reconnecting:
                return
                
            logger.info("尝试重新连接...")
            self.reconnecting = True
            reconnect_attempted = False
            
            try:
                # 关闭现有连接
                if self.ws_connected:
                    try:
                        self.ws.close()
                    except:
                        pass
                    self.ws_connected = False
                
                # 清空音频队列
                while not self.audio_queue.empty():
                    try:
                        self.audio_queue.get_nowait()
                    except queue.Empty:
                        break
                
                # 重置第一帧标志
                self.first_frame_sent = False
                
                # 等待一小段时间让连接完全关闭
                time.sleep(1)
                
                # 创建新连接
                ws_url = self._generate_url()
                self.ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=self._on_open,
                    on_message=self.on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                
                # 启动新线程
                self.ws_thread = threading.Thread(
                    target=self.ws.run_forever,
                    kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE}}
                )
                self.ws_thread.daemon = True
                self.ws_thread.start()
                
                # 重置会话开始时间
                self.session_start_time = time.time()
                reconnect_attempted = True
                
                # 重发缓存的音频数据（仅在成功建立新连接后）
                def resend_cached_after_connect():
                    start_time = time.time()
                    while time.time() - start_time < 10:  # 最多等待10秒连接建立
                        if self.ws_connected and not self.reconnecting:
                            if self.cache_enabled and self.audio_cache:
                                with self.cache_lock:
                                    cached_data = list(self.audio_cache)
                                
                                temp_queue = queue.Queue()
                                for data in cached_data:
                                    temp_queue.put(data)
                                
                                try:
                                    logger.info("开始重发缓存的音频数据...")
                                    while not temp_queue.empty() and self.is_running:
                                        data = temp_queue.get()
                                        frame = {
                                            "data": {
                                                "status": STATUS_CONTINUE_FRAME,
                                                "format": "audio/L16;rate=16000",
                                                "encoding": "raw",
                                                "audio": base64.b64encode(data).decode('utf-8')
                                            }
                                        }
                                        if self.ws_connected:
                                            self.ws.send(json.dumps(frame))
                                        time.sleep(0.02)
                                    logger.info("缓存音频数据重发完成")
                                except Exception as e:
                                    logger.error(f"重发缓存音频出错: {str(e)}")
                            break
                        time.sleep(0.1)
                    self.reconnecting = False
                
                # 确保只有一个重发线程运行
                if not hasattr(self, '_resend_thread') or not self._resend_thread.is_alive():
                    self._resend_thread = threading.Thread(
                        target=resend_cached_after_connect, 
                        daemon=True
                    )
                    self._resend_thread.start()
                
            except Exception as e:
                logger.error(f"重新连接失败: {str(e)}")
                self.reconnecting = False
                if self.auto_reconnect and self.is_running and not reconnect_attempted:
                    logger.info("5秒后再次尝试重新连接...")
                    threading.Timer(5.0, self._reconnect).start()
            finally:
                # 确保重连状态被正确重置
                if not reconnect_attempted:
                    self.reconnecting = False
    
    def _on_close(self, ws, close_status_code, close_msg):
        """WebSocket关闭回调"""
        logger.info(f"连接关闭 (code: {close_status_code}, msg: {close_msg})")
        self.ws_connected = False
        self.reconnecting = False
        
        # 只有在非重连状态下才自动重连
        if not self.reconnecting and self.auto_reconnect and self.is_running:
            self._reconnect()
    
    def _audio_capture(self):
        """从麦克风捕获音频"""
        logger.info("开始音频捕获")
        
        try:
            self.stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=self._audio_callback
            )
            self.stream.start_stream()
            
            while self.is_running and self.stream.is_active():
                time.sleep(0.1)
                
        except Exception as e:
            logger.error(f"音频捕获错误: {str(e)}")
        finally:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
            logger.info("音频捕获已停止")
    
    def _audio_callback(self, in_data, frame_count, time_info, status):
        """音频回调函数"""
        if not self.is_running:
            return (None, pyaudio.paComplete)
            
        # 将音频数据放入队列
        if not self.reconnecting:
            self.audio_queue.put(in_data)

           
        # 缓存音频数据
        if self.cache_enabled:
            with self.cache_lock:
                self.audio_cache.append(in_data)
                
        return (None, pyaudio.paContinue)
    
    def start(self):
        """启动语音识别"""
        if self.is_running:
            logger.warning("识别已在运行中")
            return
        
        logger.info("启动语音识别...")
        self.is_running = True
        self.auto_reconnect = True
        self.last_reconnect_time = 0
        
        # 创建音频队列
        self.audio_queue = queue.Queue()
        
        # 生成WebSocket URL
        ws_url = self._generate_url()
        
        # 创建WebSocket连接
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_open=self._on_open,
            on_message=self.on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )

        print(self.ws)
        
        # 启动WebSocket线程
        self.ws_thread = threading.Thread(
            target=self.ws.run_forever,
            kwargs={"sslopt": {"cert_reqs": ssl.CERT_NONE}}
        )
        self.ws_thread.daemon = True
        self.ws_thread.start()
        
        # 启动音频捕获线程
        self.audio_thread = threading.Thread(target=self._audio_capture)
        self.audio_thread.daemon = True
        self.audio_thread.start()
        
        logger.info("语音识别已启动")
        self.ws_server.start(self)
    
    def stop(self):
        """停止语音识别"""
        if not self.is_running:
            logger.warning("识别未在运行")
            return
        
        logger.info("停止语音识别...")
        self.is_running = False
        self.auto_reconnect = False
        
        # 停止音频流
        if hasattr(self, 'stream') and self.stream:
            self.stream.stop_stream()
            self.stream.close()
        
        # 关闭WebSocket
        if self.ws and self.ws_connected:
            self.ws.close()
        
        # 等待线程结束
        threads = [self.audio_thread, self.send_thread, self.ws_thread]
        for t in threads:
            if t and t.is_alive():
                t.join(timeout=1)
        
        # 清空队列
        while not self.audio_queue.empty():
            self.audio_queue.get()

        logger.info("语音识别已停止")

    def generate_emr(self):
        return self.emr_generator.generate_from_transcript(self.transcript_file)
    
    def get_result(self, timeout=None):
        """
        获取识别结果
        :param timeout: 超时时间(秒)，None表示无限等待
        :return: 识别文本或None
        """
        try:
            return self.result_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        
    
    def __enter__(self):
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        self.audio.terminate()

class XunfeiFileASR:
    def __init__(self, appid, secret_key, upload_file_path):
        self.appid = appid
        self.secret_key = secret_key
        self.upload_file_path = upload_file_path
        self.ts = str(int(time.time()))
        self.signa = self.get_signa()        

    def get_signa(self):
        appid = self.appid
        secret_key = self.secret_key
        m2 = hashlib.md5()
        m2.update((appid + self.ts).encode('utf-8'))
        md5 = m2.hexdigest()
        md5 = bytes(md5, encoding='utf-8')
        # 以secret_key为key, 上面的md5为msg， 使用hashlib.sha1加密结果为signa
        signa = hmac.new(secret_key.encode('utf-8'), md5, hashlib.sha1).digest()
        signa = base64.b64encode(signa)
        signa = str(signa, 'utf-8')
        return signa

    def upload(self):
        print("上传部分：")
        upload_file_path = self.upload_file_path
        file_len = os.path.getsize(upload_file_path)
        file_name = os.path.basename(upload_file_path)

        param_dict = {
            'appId': self.appid,
            'signa': self.signa,
            'ts': self.ts,
            "fileSize": file_len,
            "fileName": file_name,
            "duration": "200"
        }
        
        print("上传参数:", param_dict)
        
        try:
            with open(upload_file_path, 'rb') as f:
                data = f.read(file_len)
            
            print(f"正在上传 {file_len} 字节数据...")
            response = requests.post(
                url=lfasr_host + api_upload + "?" + urllib.parse.urlencode(param_dict),
                headers={"Content-type": "application/json"},
                data=data
            )
            
            print("upload_url:",response.request.url)
            result = json.loads(response.text)
            print("upload resp:", result)
            if result.get('code') != '000000':
                error_msg = result.get('descInfo', '未知错误')
                raise Exception(f"讯飞上传失败: {error_msg} (code: {result.get('code')})")
            return result
            
        except Exception as e:
            print(f"上传过程中出错: {str(e)}")
            raise

    def get_result(self):
        uploadresp = self.upload()
        orderId = uploadresp['content']['orderId']
        param_dict = {}
        param_dict['appId'] = self.appid
        param_dict['signa'] = self.signa
        param_dict['ts'] = self.ts
        param_dict['orderId'] = orderId
        param_dict['resultType'] = "transfer,predict"
        print("")
        print("查询部分：")
        print("get result参数：", param_dict)
        status = 3
        # 建议使用回调的方式查询结果，查询接口有请求频率限制
        while status == 3:
            response = requests.post(url=lfasr_host + api_get_result + "?" + urllib.parse.urlencode(param_dict),
                                     headers={"Content-type": "application/json"})
            # print("get_result_url:",response.request.url)
            result = json.loads(response.text)
            status = result['content']['orderInfo']['status']
            print("status=",status)
            if status == 4:
                break
            time.sleep(5)
        result = self._parse_result(result)
        return result
    
    def _parse_result(self, raw_result):
        """解析讯飞API返回的原始结果"""
        try:
            # 提取orderResult
            order_result = raw_result['content']['orderResult']
            if isinstance(order_result, str):
                order_result = json.loads(order_result)
            
            # 提取文本
            plain_text = ""
            if 'lattice' in order_result:
                for item in order_result['lattice']:
                    json_1best = json.loads(item['json_1best'])
                    rt = json_1best['st']['rt']
                    for word_segment in rt:
                        for word in word_segment['ws']:
                            for cw in word['cw']:
                                plain_text += cw['w']
            
            return plain_text
        
        except Exception as e:
            logger.error(f"解析讯飞结果出错: {str(e)}")
            return ""

class XunfeiDocOCR:
    def __init__(self, app_id, api_key, api_secret, deepseek_api_key):
        self.app_id = app_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.emr_generator = MedicalEMRGenerator(deepseek_api_key)  # 新增：病历生成器
        self.ocr_url = "https://cbm01.cn-huabei-1.xf-yun.com/v1/private/se75ocrbm"

    def _generate_auth_header(self):
        """生成讯飞OCR鉴权头"""
        now = datetime.now(timezone.utc)
        date = now.strftime('%a, %d %b %Y %H:%M:%S GMT')
        
        # 1. 生成signature_origin
        signature_origin = f"host: api.xf-yun.com\ndate: {date}\nPOST /v1/private/se75ocrbm HTTP/1.1"
        
        # 2. 计算签名
        signature_sha = hmac.new(
            self.api_secret.encode('utf-8'),
            signature_origin.encode('utf-8'),
            hashlib.sha256
        ).digest()
        signature = base64.b64encode(signature_sha).decode('utf-8')
        
        # 3. 构造authorization
        authorization_origin = (
            f'api_key="{self.api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{signature}"'
        )
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode('utf-8')
        
        return {
            "host": "api.xf-yun.com",
            "date": date,
            "authorization": authorization
        }

    def recognize_document(self, image_path):
        """
        识别医疗文档图片/PDF
        :param image_path: 图片文件路径
        :return: 识别文本（已结构化处理）
        """
        try:
            # 1. 读取并编码图片
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode('utf-8')
            
            # 获取文件扩展名
            file_ext = os.path.splitext(image_path)[1][1:].lower()  # 去掉点
            
            # 2. 构建请求体 - 按照Java版格式
            payload = {
                "header": {
                    "app_id": self.app_id,
                    "uid": "12345",
                    "did": "iocr",
                    "net_type": "wifi",
                    "net_isp": "CMCC",
                    "status": 0,
                    "request_id": None,
                    "res_id": ""
                },
                "parameter": {
                    "ocr": {
                        "result_option": "normal",
                        "result_format": "json,markdown",
                        "output_type": "one_shot",
                        "exif_option": "1",
                        "json_element_option": "",
                        "markdown_element_option": "watermark=1,page_header=1,page_footer=1,page_number=1,graph=1",
                        "sed_element_option": "watermark=0,page_header=0,page_footer=0,page_number=0,graph=0",
                        "alpha_option": "0",
                        "rotation_min_angle": 5,
                        "result": {
                            "encoding": "utf8",
                            "compress": "raw",
                            "format": "plain"
                        }
                    }
                },
                "payload": {
                    "image": {
                        "encoding": file_ext,
                        "image": image_data,
                        "status": 0,
                        "seq": 0
                    }
                }
            }
            
            # 3. 获取鉴权头
            headers = self._generate_auth_header()
            params = {
                "authorization": headers["authorization"],
                "date": headers["date"],
                "host": headers["host"]
            }
            
            # 4. 发送请求
            response = requests.post(
                self.ocr_url,
                params=params,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
                
            # 4. 使用通用解析方法
            decoded_text=self.parse_response(response.text)
            cleaned_text=self.extract_text_from_ocr_result(decoded_text)
            return  cleaned_text

        except Exception as e:
            logger.error(f"文档识别失败: {str(e)}")
            raise

    def generate_emr(self, image_path):
        """识别医疗文档并生成结构化病历（EMR）"""
        try:
            # 1. OCR识别文本
            ocr_text = self.recognize_document(image_path)
            
            # 2. 调用DeepSeek生成结构化病历
            emr_data = self.emr_generator.generate_from_image(ocr_text)
            
            return emr_data

        except Exception as e:
            logger.error(f"生成病历失败: {str(e)}")
            return {
                "主诉": "OCR识别失败",
                "现病史": "请检查图片清晰度或重新上传",
                "既往史": "",
                "体征": "",
                "实验室检查结果": "",
                "医生建议": ""
            }

    def parse_response(self, response):
        """
        通用解析方法，仅解码讯飞OCR返回的原始结果
        :param response: OCR API返回的原始响应
        :return: 解码后的文本内容
        """
        try:
            # 解析JSON响应
            resp_data = json.loads(response)
            
            # 检查错误码
            header = resp_data.get("header", {})
            if header.get("code") != 0:
                error_msg = header.get("message", "未知错误")
                raise Exception(f"OCR API错误 (code: {header.get('code')}): {error_msg}")
            
            # 提取并解码文本内容
            encoded_text = resp_data["payload"]["result"]["text"]
            decoded_text = base64.b64decode(encoded_text).decode("utf-8")
            
            return decoded_text
            
        except Exception as e:
            logger.error(f"解析OCR响应失败: {str(e)}")
            raise Exception(f"解析OCR响应失败: {str(e)}")
        
    def extract_text_from_ocr_result(self,ocr_json):
        """
        从OCR识别结果中提取纯文本内容
        :param ocr_json: OCR识别结果的JSON字符串或字典
        :return: 整理后的纯文本字符串
        """
        if isinstance(ocr_json, str):
            try:
                data = json.loads(ocr_json)
            except json.JSONDecodeError:
                return "Invalid JSON format"
        else:
            data = ocr_json
        
        # 提取markdown格式的内容
        markdown_text = ""
        for doc in data.get("document", []):
            if doc.get("name") == "markdown":
                markdown_text = doc.get("value", "")
                break
        
        # 清理多余的空格和换行
        cleaned_text = "\n".join(line.strip() for line in markdown_text.splitlines() if line.strip())
        print(cleaned_text)
        return cleaned_text

def main():
    # 替换为您的实际API信息
    APP_ID = '8a50f934'
    XUNFEI_API_KEY = '4d50db64ae1809e3a3fbca2ed396617c'
    XUNFEI_API_SECRET = 'NzYyYWI4ODg3OGU2ZmEzYjQ3ZDdmYTA2'
    XUNFEI_SECRET_KEY='bad35de9c6315d7d4cf39482ed5ffad2'
    
    # ✅ 替换为您的DeepSeek API Key
    DEEPSEEK_API_KEY = 'sk-10db7b77c83241dea1ae0c35256c4554'
    
    # 创建语音识别实例
    asr = XunfeiStreamASR(
        APP_ID, 
        XUNFEI_API_KEY, 
        XUNFEI_API_SECRET,
        XUNFEI_SECRET_KEY,
        DEEPSEEK_API_KEY
    )
    
    webs = WebSocketServer()
    try:
        # 启动识别
        # asr.start()
        webs.start(asr)
        print(f"WebSocket服务器已在 ws://localhost:8765 启动")
        print("等待前端连接... (按Ctrl+C停止)")
        
        # 保持主线程运行
        while True:
            time.sleep(1)
    
    except KeyboardInterrupt:
        print("\n停止识别...")
    finally:
        asr.stop()

# 示例使用
if __name__ == "__main__":
    main()