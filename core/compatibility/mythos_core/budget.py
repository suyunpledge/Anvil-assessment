"""无 tokenizer 依赖的保守请求估算；只用于超预算预检，不静默截断源码。"""

import json
import math


def estimate_request_tokens(messages, tools=None):
    text = json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False,
                      separators=(",", ":"))
    non_ascii = sum(ord(char) > 127 for char in text)
    return math.ceil((len(text) - non_ascii) / 3) + non_ascii * 2 + 64 + 16 * len(messages)
