"""DeepSeek Chat Completions. Credentials stay in process memory only."""
import json
import os
import ssl
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ENDPOINT='https://api.deepseek.com/chat/completions'
CONFIG={'model':'deepseek-flash','max_tokens':8192,'thinking':{'type':'enabled'},'reasoning_effort':'max','stream':False}


def credential_status():
    return 'environment' if os.environ.get('DEEPSEEK_API_KEY', '').strip() else 'missing'


def load_key():
    key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    if not key:
        raise RuntimeError('未配置模型密钥。请设置DEEPSEEK_API_KEY后重启服务。')
    return key


def generate(messages):
    payload={**CONFIG,'messages':messages}
    key=load_key()
    request=Request(ENDPOINT,data=json.dumps(payload,ensure_ascii=False).encode(),
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
    started=time.monotonic()
    try:
        if os.environ.get('SSL_CERT_FILE'):
            context=ssl.create_default_context()
        else:
            try:
                import certifi
                context=ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                context=ssl.create_default_context()
        with urlopen(request,timeout=180,context=context) as response:
            raw_text=response.read().decode('utf-8');raw=json.loads(raw_text)
    except HTTPError as error:
        raise RuntimeError(f'模型接口返回HTTP {error.code}。未自动重试，请检查额度、模型名或稍后重试。') from None
    except (URLError,TimeoutError,OSError):
        raise RuntimeError('模型网络请求失败或超时；未自动重试，该请求可能已计费。') from None
    except (ValueError,UnicodeDecodeError):
        raise RuntimeError('模型接口返回的内容无法解析；未生成替代答案。') from None
    if not isinstance(raw,dict) or not raw.get('choices'):raise RuntimeError('模型未返回可用choices；未生成替代答案。')
    choice=raw['choices'][0];content=choice.get('message',{}).get('content')
    if content is None:content=''
    if not isinstance(content,str):raise RuntimeError('模型答案不是文本。')
    return {'content':content,'finish_reason':choice.get('finish_reason'),'usage':raw.get('usage',{}),
            'response_model':raw.get('model'),'response_id':raw.get('id'),
            'elapsed_seconds':round(time.monotonic()-started,3),'raw':raw,'raw_text':raw_text,
            'payload':payload,'endpoint':ENDPOINT}
