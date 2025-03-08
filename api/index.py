from fastapi import FastAPI, Request, Response, HTTPException, Depends, Header, Security, status, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import httpx
import json
import random
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Union
import os
import redis
from datetime import datetime
import pathlib
import copy
import logging
import asyncio

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger("uniapi")

# 获取当前文件的目录
BASE_DIR = pathlib.Path(__file__).parent.resolve()

app = FastAPI(title="UniAPI - OpenAI API转发器")

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# 配置模板
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 配置安全访问密钥，使用标准Bearer Token认证
security = HTTPBearer(auto_error=False)

# 从环境变量获取允许的API密钥
ALLOWED_API_KEYS = []
for i in range(1, 6):  # 最多支持5个API密钥
    key_env_var = f"API_KEY_{i}"
    if key_env_var in os.environ and os.environ[key_env_var]:
        ALLOWED_API_KEYS.append(os.environ[key_env_var])

# 获取管理员API密钥
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "adminadmin")

# 获取HTTP请求超时设置
TIMEOUT_SECONDS = float(os.environ.get("TIMEOUT_SECONDS", "60"))
logger.info(f"HTTP请求超时设置为 {TIMEOUT_SECONDS} 秒")

# 默认开发API密钥（只用于本地开发，在生产环境中应该通过环境变量设置）
if not ALLOWED_API_KEYS and os.environ.get("ENVIRONMENT") != "production":
    ALLOWED_API_KEYS = ["dev_api_key_1", "dev_api_key_2"]

# 验证API密钥
async def verify_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    if not ALLOWED_API_KEYS and not ADMIN_API_KEY:
        # 如果没有配置任何API密钥，则禁止所有访问
        raise HTTPException(
            status_code=401,
            detail="未配置允许的API密钥。请在环境变量中设置API_KEY_1至API_KEY_5或ADMIN_API_KEY"
        )
    
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="缺少认证信息。请使用'Authorization: Bearer YOUR_API_KEY'格式提供访问密钥"
        )
    
    api_key = credentials.credentials  # 从Bearer Token中提取API密钥
    
    # 检查是否是管理员API密钥
    if api_key == ADMIN_API_KEY:
        return api_key
    
    # 检查是否是普通API密钥
    if api_key in ALLOWED_API_KEYS:
        # 获取当前请求路径
        request = credentials.scope.get("request")
        if request:
            path = request.url.path
            # 如果是管理相关的API，拒绝访问
            if path.startswith("/admin") or path.startswith("/api/configs") or path.startswith("/api/model-mappings"):
                raise HTTPException(
                    status_code=403,
                    detail="普通API密钥无权访问管理功能"
                )
        return api_key
    
    raise HTTPException(
        status_code=401,
        detail="无效的API密钥"
    )

# 验证管理员API密钥
async def verify_admin_api_key(credentials: HTTPAuthorizationCredentials = Security(security)):
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="缺少认证信息。请使用'Authorization: Bearer YOUR_API_KEY'格式提供访问密钥"
        )
    
    api_key = credentials.credentials
    if api_key != ADMIN_API_KEY:
        raise HTTPException(
            status_code=403,
            detail="需要管理员权限"
        )
    
    return api_key

# 初始化Redis客户端
redis_url = os.getenv("REDIS_URL")
if redis_client := redis.from_url(redis_url) if redis_url else None:
    pass
else:
    # 使用内存存储作为备用
    in_memory_db = {
        "api_configs": [],
        "model_mappings": {}  # 新增模型映射存储
    }

# 模型映射数据模型
class ModelMapping(BaseModel):
    unified_name: str  # 统一的模型名称（用于外部调用）
    vendor_models: Dict[str, str]  # 厂商特定的模型名称映射，格式: {vendor_id: model_name}

# API配置数据模型
class APIConfig(BaseModel):
    id: Optional[str] = None
    api_key: str
    base_url: str
    models: List[str] = Field(..., description="支持的模型列表")
    created_at: Optional[str] = None
    vendor: Optional[str] = Field(None, description="厂商标识符，用于模型映射")
    model_mappings: Optional[Dict[str, str]] = Field(None, description="模型映射，格式: {统一模型名称: 实际模型名称}")

# 模型映射请求模型
class ModelMappingRequest(BaseModel):
    unified_name: str
    vendor_models: Dict[str, str]

# API配置相关端点
@app.post("/logout")
async def logout():
    response = JSONResponse({"status": "success"})
    response.delete_cookie(key="auth_key")
    response.delete_cookie(key="remember_auth")
    return response

# 使用cookie获取管理员密钥
async def get_admin_api_key_from_cookie(request: Request):
    auth_key = request.cookies.get("auth_key")
    if not auth_key or auth_key != ADMIN_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未授权，请使用管理员API密钥访问此页面"
        )
    return auth_key

@app.post("/api/configs")
async def create_config(config: APIConfig, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """创建新的API配置"""
    config_dict = config.model_dump()
    config_dict["id"] = datetime.now().strftime("%Y%m%d%H%M%S")
    config_dict["created_at"] = datetime.now().isoformat()
    
    # 如果未指定厂商标识符，使用base_url的域名作为厂商标识符
    if not config_dict.get("vendor"):
        from urllib.parse import urlparse
        parsed_url = urlparse(config_dict["base_url"])
        config_dict["vendor"] = parsed_url.netloc
    
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
        configs.append(config_dict)
        redis_client.set("api_configs", json.dumps(configs))
    else:
        in_memory_db["api_configs"].append(config_dict)
    
    return {"message": "配置已创建", "config_id": config_dict["id"]}

@app.put("/api/configs/{config_id}")
async def update_config(config_id: str, config: APIConfig, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """更新现有的API配置"""
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
    else:
        configs = in_memory_db["api_configs"]
    
    # 查找要更新的配置
    found = False
    for i, existing_config in enumerate(configs):
        if existing_config["id"] == config_id:
            # 保留原有的id和created_at
            config_dict = config.model_dump()
            config_dict["id"] = config_id
            config_dict["created_at"] = existing_config.get("created_at", datetime.now().isoformat())
            
            # 如果未指定厂商标识符，保留原有的或使用base_url的域名
            if not config_dict.get("vendor"):
                config_dict["vendor"] = existing_config.get("vendor")
                if not config_dict["vendor"]:
                    from urllib.parse import urlparse
                    parsed_url = urlparse(config_dict["base_url"])
                    config_dict["vendor"] = parsed_url.netloc
            
            configs[i] = config_dict
            found = True
            break
    
    if not found:
        raise HTTPException(status_code=404, detail=f"未找到ID为{config_id}的配置")
    
    # 保存更新后的配置
    if redis_client:
        redis_client.set("api_configs", json.dumps(configs))
    else:
        in_memory_db["api_configs"] = configs
    
    return {"message": "配置已更新", "config_id": config_id}

@app.get("/api/configs")
async def list_configs(api_key: str = Depends(get_admin_api_key_from_cookie)):
    """列出所有API配置"""
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
    else:
        configs = in_memory_db["api_configs"]
    
    # 创建配置的深拷贝，避免修改原始数据
    configs_for_display = copy.deepcopy(configs)
    
    # 隐藏API密钥（仅在显示时）
    for config in configs_for_display:
        config["api_key"] = "**" + config["api_key"][-4:] if len(config["api_key"]) > 4 else "****"
    
    return {"configs": configs_for_display}

@app.get("/api/configs/{config_id}")
async def get_config(config_id: str, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """获取单个API配置的详细信息"""
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
    else:
        configs = in_memory_db["api_configs"]
    
    # 查找指定的配置
    for config in configs:
        if config["id"] == config_id:
            # 创建配置的深拷贝，避免修改原始数据
            config_copy = copy.deepcopy(config)
            # 隐藏API密钥（仅在显示时）
            config_copy["api_key"] = "**" + config_copy["api_key"][-4:] if len(config_copy["api_key"]) > 4 else "****"
            return config_copy
    
    raise HTTPException(status_code=404, detail=f"未找到ID为{config_id}的配置")

@app.delete("/api/configs/{config_id}")
async def delete_config(config_id: str, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """删除指定的API配置"""
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
        configs = [c for c in configs if c["id"] != config_id]
        redis_client.set("api_configs", json.dumps(configs))
    else:
        in_memory_db["api_configs"] = [c for c in in_memory_db["api_configs"] if c["id"] != config_id]
    
    return {"message": "配置已删除"}

# 模型映射相关端点
@app.post("/api/model-mappings")
async def create_model_mapping(mapping: ModelMappingRequest, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """创建或更新模型映射"""
    if redis_client:
        mappings = json.loads(redis_client.get("model_mappings") or "{}")
        mappings[mapping.unified_name] = mapping.vendor_models
        redis_client.set("model_mappings", json.dumps(mappings))
    else:
        if "model_mappings" not in in_memory_db:
            in_memory_db["model_mappings"] = {}
        in_memory_db["model_mappings"][mapping.unified_name] = mapping.vendor_models
    
    return {"message": f"模型映射已创建: {mapping.unified_name}"}

@app.get("/api/model-mappings")
async def list_model_mappings(api_key: str = Depends(get_admin_api_key_from_cookie)):
    """列出所有模型映射"""
    if redis_client:
        mappings = json.loads(redis_client.get("model_mappings") or "{}")
    else:
        mappings = in_memory_db.get("model_mappings", {})
    
    return {"mappings": mappings}

@app.delete("/api/model-mappings/{unified_name}")
async def delete_model_mapping(unified_name: str, api_key: str = Depends(get_admin_api_key_from_cookie)):
    """删除指定的模型映射"""
    if redis_client:
        mappings = json.loads(redis_client.get("model_mappings") or "{}")
        if unified_name in mappings:
            del mappings[unified_name]
            redis_client.set("model_mappings", json.dumps(mappings))
    else:
        if "model_mappings" in in_memory_db and unified_name in in_memory_db["model_mappings"]:
            del in_memory_db["model_mappings"][unified_name]
    
    return {"message": f"模型映射已删除: {unified_name}"}

# 获取指定模型的随机配置
def get_random_config_for_model(model: str):
    logger.info(f"查找模型配置: {model}")
    
    if redis_client:
        configs = json.loads(redis_client.get("api_configs") or "[]")
        mappings = json.loads(redis_client.get("model_mappings") or "{}")
    else:
        configs = in_memory_db["api_configs"]
        mappings = in_memory_db.get("model_mappings", {})
    
    logger.info(f"加载了 {len(configs)} 个配置和 {len(mappings)} 个全局映射")
    
    # 首先，检查是否有配置直接包含该模型的映射
    configs_with_direct_mapping = []
    for config in configs:
        # 检查配置中的模型映射
        if config.get("model_mappings") and model in config["model_mappings"]:
            # 找到了直接映射，使用这个配置
            actual_model = config["model_mappings"][model]
            # 确保配置支持实际模型
            if actual_model in config["models"]:
                configs_with_direct_mapping.append((config, actual_model))
                logger.info(f"找到配置内映射: {model} -> {actual_model} (配置ID: {config.get('id', 'unknown')})")
    
    # 如果找到了直接映射的配置，随机选择一个
    if configs_with_direct_mapping:
        selected = random.choice(configs_with_direct_mapping)
        logger.info(f"使用配置内映射: {model} -> {selected[1]} (配置ID: {selected[0].get('id', 'unknown')})")
        return selected
    
    # 其次，检查全局模型映射
    if model in mappings:
        logger.info(f"找到全局映射: {model} -> {mappings[model]}")
        # 为每个可用的厂商找到可用的配置
        vendor_configs = []
        for vendor, vendor_model in mappings[model].items():
            # 找到该厂商的配置，并且该配置支持该模型
            matching_vendor_configs = [
                c for c in configs 
                if c.get("vendor") == vendor and vendor_model in c["models"]
            ]
            for config in matching_vendor_configs:
                vendor_configs.append((config, vendor_model))
                logger.info(f"找到厂商映射: {vendor} -> {vendor_model} (配置ID: {config.get('id', 'unknown')})")
        
        if vendor_configs:
            # 随机选择一个配置和对应的模型
            selected = random.choice(vendor_configs)
            logger.info(f"使用厂商映射: {model} -> {selected[1]} (配置ID: {selected[0].get('id', 'unknown')})")
            return selected
    
    # 最后，查找直接支持该模型的配置
    matching_configs = [(c, model) for c in configs if model in c["models"]]
    if not matching_configs:
        logger.warning(f"没有找到支持模型 {model} 的配置")
        raise HTTPException(status_code=404, detail=f"没有找到支持模型 {model} 的配置")
    
    selected = random.choice(matching_configs)
    logger.info(f"使用直接匹配: {model} (配置ID: {selected[0].get('id', 'unknown')})")
    return selected

# OpenAI兼容端点
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def openai_proxy(path: str, request: Request):
    """OpenAI API兼容代理"""
    # 验证API密钥（从请求头中获取）
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail="缺少认证信息。请使用'Authorization: Bearer YOUR_API_KEY'格式提供访问密钥"
        )
    
    try:
        # 提取API密钥
        scheme, credentials = auth_header.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="认证格式错误。请使用'Authorization: Bearer YOUR_API_KEY'格式"
            )
        api_key = credentials
        
        # 检查API密钥有效性
        if not ALLOWED_API_KEYS and not ADMIN_API_KEY:
            raise HTTPException(
                status_code=401,
                detail="未配置允许的API密钥。请在环境变量中设置API_KEY_1至API_KEY_5或ADMIN_API_KEY"
            )
        
        # 验证API密钥
        if api_key != ADMIN_API_KEY and api_key not in ALLOWED_API_KEYS:
            raise HTTPException(
                status_code=401,
                detail="无效的API密钥"
            )
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="认证格式错误。请使用'Authorization: Bearer YOUR_API_KEY'格式"
        )
    
    # 只处理 chat/completions 请求
    if path != "chat/completions":
        raise HTTPException(status_code=404, detail="端点不存在")
    
    # 获取请求内容
    body = await request.body()
    body_dict = json.loads(body) if body else {}
    
    # 获取模型名称
    model = body_dict.get("model", "")
    if not model:
        raise HTTPException(status_code=400, detail="请求中未指定模型")
    
    logger.info(f"收到API请求: 路径={path}, 模型={model}")
    
    try:
        # 随机选择配置，并获取实际模型名称
        config, actual_model = get_random_config_for_model(model)
        logger.info(f"选择配置: ID={config.get('id', 'unknown')}, 实际模型={actual_model}")
        
        # 如果实际模型名称与请求的不同，替换请求中的模型名称
        if actual_model != model:
            logger.info(f"模型映射: {model} -> {actual_model}")
            body_dict["model"] = actual_model
            body = json.dumps(body_dict).encode()
        
        # 准备转发
        headers = dict(request.headers)
        headers.pop("host", None)
        
        # 移除所有形式的authorization头（不区分大小写）
        auth_headers = [k for k in headers.keys() if k.lower() == "authorization"]
        for key in auth_headers:
            headers.pop(key)
        
        # 更新Content-Length头以匹配新的请求体长度
        content_length_headers = [k for k in headers.keys() if k.lower() == "content-length"]
        for key in content_length_headers:
            headers.pop(key)
        
        # 添加新的Content-Length头
        if body:
            headers["Content-Length"] = str(len(body))
        
        # 添加正确的Authorization头
        headers["Authorization"] = f"Bearer {config['api_key']}"
        
        # 设置正确的base_url和请求URL
        base_url = config["base_url"]
        if base_url.endswith("#"):
            # 如果以#结尾，移除#后直接使用
            url = base_url[:-1]
        elif base_url.endswith("/"):
            # 如果以/结尾，直接拼接chat/completions
            url = f"{base_url}chat/completions"
        else:
            # 默认拼接完整路径
            url = f"{base_url}/v1/chat/completions"
        
        logger.info(f"转发请求到: {url}")
        
        # 判断是否为流式请求
        is_stream = body_dict.get("stream", False)
        
        if is_stream:
            async def stream_generator():
                client = httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT_SECONDS)
                try:
                    async with client.stream(
                        request.method,
                        url,
                        headers=headers,
                        content=body
                    ) as response:
                        # 检查状态码
                        if not response.is_success:
                            error_content = await response.read()
                            yield error_content
                            return
                        
                        # 直接逐块读取和输出内容
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                yield chunk
                except Exception as e:
                    logger.error(f"流式处理出错: {str(e)}")
                    yield f"data: {{\"error\":\"流式处理出错: {str(e)}\"}}\n\n".encode()
                finally:
                    await client.aclose()
            
            # 返回流式响应
            return StreamingResponse(
                stream_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Content-Type": "text/event-stream"
                }
            )
        else:
            # 非流式请求的处理
            async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT_SECONDS) as client:
                response = await client.request(
                    request.method,
                    url,
                    headers=headers,
                    content=body
                )
                
                # 直接返回响应内容
                return JSONResponse(
                    content=response.json(),
                    status_code=response.status_code,
                    headers=dict(response.headers)
                )
    except Exception as e:
        # 添加详细的错误日志
        logger.error(f"处理请求时出错: {str(e)}")
        # 返回友好的错误信息
        return JSONResponse(
            content={"error": "处理请求时出错", "message": str(e)},
            status_code=500
        )

# 页面路由

# 主页 - 重定向到登录页
@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

# 登录页面
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/admin", response_class=HTMLResponse)
async def admin_login(request: Request, api_key: str = Form(...), remember_me: Optional[bool] = Form(False)):
    # 验证API密钥
    if api_key == ADMIN_API_KEY:
        # 设置cookie而不是返回JSON
        response = templates.TemplateResponse("admin.html", {"request": request})
        response.set_cookie(key="auth_key", value=api_key, httponly=True)
        # 如果选择记住密钥，设置更长的过期时间
        if remember_me:
            response.set_cookie(key="remember_auth", value="true", max_age=30*24*60*60, httponly=True)
        return response
    else:
        # 验证失败，返回错误信息
        return templates.TemplateResponse("login.html", {
            "request": request, 
            "error": "无效的API密钥或无权访问管理面板"
        })

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    # 从cookie中获取API密钥
    auth_key = request.cookies.get("auth_key")
    if not auth_key or auth_key != ADMIN_API_KEY:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse("admin.html", {"request": request})

# 健康检查端点
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# 获取用于调试的API密钥
async def get_api_key_from_request(request: Request):
    """从请求头中提取API密钥"""
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail="缺少认证信息。请使用'Authorization: Bearer YOUR_API_KEY'格式提供访问密钥"
        )
    
    try:
        # 提取API密钥
        scheme, credentials = auth_header.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="认证格式错误。请使用'Authorization: Bearer YOUR_API_KEY'格式"
            )
        api_key = credentials
        
        # 检查API密钥有效性
        if not ALLOWED_API_KEYS and not ADMIN_API_KEY:
            raise HTTPException(
                status_code=401,
                detail="未配置允许的API密钥。请在环境变量中设置API_KEY_1至API_KEY_5或ADMIN_API_KEY"
            )
        
        # 验证API密钥
        if api_key != ADMIN_API_KEY and api_key not in ALLOWED_API_KEYS:
            raise HTTPException(
                status_code=401,
                detail="无效的API密钥"
            )
            
        return api_key
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="认证格式错误。请使用'Authorization: Bearer YOUR_API_KEY'格式"
        )

@app.get("/api/debug/model-mapping/{model}")
async def debug_model_mapping(model: str, request: Request):
    """调试模型映射"""
    await get_api_key_from_request(request)  # 验证API密钥
    try:
        config, mapped_model = get_random_config_for_model(model)
        return {
            "model": model,
            "mapped_model": mapped_model,
            "config": {
                "id": config.get("id"),
                "base_url": config.get("base_url"),
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/debug/forward")
async def debug_forward(request: Request):
    """调试转发请求"""
    await get_api_key_from_request(request)  # 验证API密钥
    try:
        # 获取请求内容
        body = await request.body()
        body_dict = json.loads(body) if body else {}
        
        # 获取模型名称和目标URL
        model = body_dict.get("model", "")
        target_url = body_dict.get("target_url", "")
        
        if not model or not target_url:
            return {
                "error": "缺少必要参数",
                "message": "请提供model和target_url参数"
            }
        
        # 随机选择配置，并获取实际模型名称
        config, actual_model = get_random_config_for_model(model)
        
        # 如果实际模型名称与请求的不同，替换请求中的模型名称
        if actual_model != model:
            body_dict["model"] = actual_model
            body = json.dumps(body_dict).encode()
        
        # 准备转发
        headers = dict(request.headers)
        headers.pop("host", None)
        
        # 移除所有形式的authorization头（不区分大小写）
        auth_headers = [k for k in headers.keys() if k.lower() == "authorization"]
        for key in auth_headers:
            headers.pop(key)
        
        # 更新Content-Length头以匹配新的请求体长度
        content_length_headers = [k for k in headers.keys() if k.lower() == "content-length"]
        for key in content_length_headers:
            headers.pop(key)
        
        # 添加新的Content-Length头
        if body:
            headers["Content-Length"] = str(len(body))
        
        # 添加正确的Authorization头
        headers["Authorization"] = f"Bearer {config['api_key']}"
        
        # 发送请求
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                target_url,
                headers=headers,
                content=body
            )
            
            # 返回详细信息
            return {
                "request": {
                    "url": target_url,
                    "headers": {k: v for k, v in headers.items() if k.lower() != "authorization"},
                    "body_length": len(body) if body else 0,
                    "body_preview": str(body[:200]) + "..." if body and len(body) > 200 else str(body)
                },
                "response": {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body_length": len(response.content),
                    "body_preview": str(response.content[:200]) + "..." if len(response.content) > 200 else str(response.content)
                },
                "mapping": {
                    "original_model": model,
                    "actual_model": actual_model,
                    "config_id": config.get("id", "unknown")
                }
            }
    except Exception as e:
        logger.error(f"调试转发时出错: {str(e)}", exc_info=True)
        return {
            "error": "调试转发时出错",
            "message": str(e)
        }

@app.post("/api/debug/stream")
async def debug_stream(request: Request):
    """调试流式请求"""
    await get_api_key_from_request(request)  # 验证API密钥
    try:
        # 获取请求内容
        body = await request.body()
        body_dict = json.loads(body) if body else {}
        
        # 确保请求中包含stream=True
        body_dict["stream"] = True
        
        # 获取模型名称和目标URL
        model = body_dict.get("model", "")
        target_url = body_dict.get("target_url", "")
        
        if not model or not target_url:
            return {
                "error": "缺少必要参数",
                "message": "请提供model和target_url参数"
            }
        
        # 随机选择配置，并获取实际模型名称
        config, actual_model = get_random_config_for_model(model)
        
        # 如果实际模型名称与请求的不同，替换请求中的模型名称
        if actual_model != model:
            body_dict["model"] = actual_model
        
        # 重新编码请求体
        body = json.dumps(body_dict).encode()
        
        # 准备转发
        headers = dict(request.headers)
        headers.pop("host", None)
        
        # 移除所有形式的authorization头（不区分大小写）
        auth_headers = [k for k in headers.keys() if k.lower() == "authorization"]
        for key in auth_headers:
            headers.pop(key)
        
        # 更新Content-Length头以匹配新的请求体长度
        content_length_headers = [k for k in headers.keys() if k.lower() == "content-length"]
        for key in content_length_headers:
            headers.pop(key)
        
        # 添加新的Content-Length头
        headers["Content-Length"] = str(len(body))
        
        # 添加正确的Authorization头
        headers["Authorization"] = f"Bearer {config['api_key']}"
        
        # 设置正确的Content-Type
        headers["Content-Type"] = "application/json"
        
        async def stream_generator():
            client = httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT_SECONDS)
            try:
                async with client.stream(
                    "POST",
                    target_url,
                    headers=headers,
                    json=body_dict
                ) as response:
                    # 检查状态码
                    if not response.is_success:
                        error_content = await response.read()
                        yield error_content
                        return
                    
                    # 直接逐块读取和输出内容
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yield chunk
            except Exception as e:
                logger.error(f"流式测试出错: {str(e)}")
                yield f"data: {{\"error\":\"流式测试出错: {str(e)}\"}}\n\n".encode()
            finally:
                await client.aclose()
        
        # 返回流式响应
        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": "text/event-stream"
            }
        )
    except Exception as e:
        logger.error(f"流式测试出错: {str(e)}")
        return JSONResponse(
            content={"error": "流式测试出错", "message": str(e)},
            status_code=500
        )

# 流式响应包装器
async def stream_with_logging(stream):
    """包装流式响应，添加日志记录"""
    chunk_count = 0
    total_bytes = 0
    
    try:
        async for chunk in stream:
            chunk_count += 1
            total_bytes += len(chunk)
            if chunk_count % 10 == 0:  # 每10个块记录一次日志，避免日志过多
                logger.info(f"已传输 {chunk_count} 个块，共 {total_bytes} 字节")
            yield chunk
        
        logger.info(f"流式传输完成，共 {chunk_count} 个块，{total_bytes} 字节")
    except Exception as e:
        logger.error(f"流式传输出错: {str(e)}", exc_info=True)
        raise
