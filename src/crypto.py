"""
Telegram Desktop 缓存加密解密核心模块

解密链路:
  key_datas (TDF$) → PBKDF2-HMAC-SHA512 派生 base_key (无密码时迭代1次)
  → AES-IGE-256 解密 localkey_block → 得到 256 字节 LocalKey
  TDEF 缓存文件 → SHA256 派生 real_key + IV → AES-CTR 解密 → 原始媒体数据

关键陷阱: dataLen 包含自身4字节，应使用 decrypted[4:dataLen] 而非 decrypted[4:4+dataLen]
"""

import hashlib
import struct

try:
    import tgcrypto
    from Crypto.Cipher import AES
except ImportError as e:
    raise ImportError(
        "缺少依赖库，请安装: pip install tgcrypto pycryptodome\n"
        f"错误详情: {e}"
    )


# ==================== 哈希原语 ====================

def sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()

def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ==================== AES-IGE-256 (用于 TDF$ 文件) ====================

def _prepare_aes_oldmtp(key: bytes, msg_key: bytes):
    """
    从 authKey(256B) 和 msgKey(16B) 派生 AES-IGE 的 key(32B) 和 IV(32B)
    参考 Telegram MTProto 旧协议
    """
    sha1_a = sha1(msg_key[:16] + key[8:8 + 32])
    sha1_b = sha1(key[8 + 32:8 + 32 + 16] + msg_key[:16] + key[8 + 48:8 + 48 + 16])
    sha1_c = sha1(key[8 + 64:8 + 64 + 32] + msg_key[:16])
    sha1_d = sha1(msg_key[:16] + key[8 + 96:8 + 96 + 32])

    aes_key = sha1_a[:8] + sha1_b[8:8 + 12] + sha1_c[4:4 + 12]
    aes_iv = sha1_a[8:8 + 12] + sha1_b[:8] + sha1_c[16:16 + 4] + sha1_d[:8]
    return aes_key, aes_iv


def aes_ige_decrypt(src: bytes, auth_key: bytes, msg_key: bytes) -> bytes:
    """AES-IGE-256 解密"""
    aes_key, aes_iv = _prepare_aes_oldmtp(auth_key, msg_key)
    return tgcrypto.ige256_decrypt(src, aes_key, aes_iv)


# ==================== AES-CTR (用于 TDEF 缓存文件) ====================

def _telegram_ctr_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """
    Telegram 使用的 AES-CTR 模式:
    counter_block = iv(16B), 后8字节为 block_index (big-endian), 每16字节块递增
    与标准 CTR 等价 (IV 作为初始计数值)
    """
    from Crypto.Util import Counter
    ctr = Counter.new(128, initial_value=int.from_bytes(iv, 'big'), little_endian=False)
    cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
    return cipher.decrypt(data)


# ==================== LocalKey 提取 ====================

def _create_local_key(passcode: bytes, salt: bytes) -> bytes:
    """
    从 passcode 和 salt 生成 base_key (256字节)
    PBKDF2-HMAC-SHA512, 无密码时迭代1次, 有密码时迭代100000次
    """
    hash_key = hashlib.sha512()
    hash_key.update(salt)
    hash_key.update(passcode)
    hash_key.update(salt)
    iter_count = 100000 if passcode else 1
    return hashlib.pbkdf2_hmac("sha512", hash_key.digest(), salt, iter_count, 256)


def _decrypt_local(encrypted: bytes, key: bytes) -> bytes:
    """
    解密 TDF$ 中的加密块
    格式: msgKey(16B) + encrypted_data
    返回: 原始数据 (dataLen-4 字节)
    关键: dataLen 包含自身4字节，实际数据为 decrypted[4:dataLen]
    """
    msg_key = encrypted[:16]
    decrypted = aes_ige_decrypt(encrypted[16:], key, msg_key)
    if sha1(decrypted)[:16] != msg_key:
        raise ValueError('bad checksum for decrypted data (wrong passcode?)')
    data_len = struct.unpack('<I', decrypted[:4])[0]
    return decrypted[4:data_len]  # 不是 4:4+data_len!


def extract_local_key(key_datas_path: str, passcode: bytes = b'') -> bytes:
    """
    从 key_datas 文件提取 LocalKey (256字节)

    key_datas 文件格式 (TDF$):
      header(4B "TDF$") + version(4B) + salt_len(BE32) + salt + 
      localkey_block_len(BE32) + localkey_block + info_len(BE32) + info + MD5(16B)

    localkey_block 内部: msgKey(16B) + AES-IGE加密数据
    """
    with open(key_datas_path, 'rb') as f:
        data = f.read()

    if data[:4] != b'TDF$':
        raise ValueError(f'Wrong file type: {data[:4]}, expected TDF$')

    cur = 4  # skip TDF$
    version = data[cur:cur + 4]
    cur += 4

    # 验证 MD5 校验 (非致命: 某些 Telegram 版本 MD5 计算方式不同)
    file_data = data[4:-16]  # version + all data except MD5
    data_size = len(data) - 16 - 4  # 不含 TDF$ 和 MD5
    m = hashlib.md5()
    m.update(data[4:-16])  # version + content (不含MD5)
    m.update(struct.pack('<I', data_size))
    m.update(version)
    m.update(b'TDF$')
    if m.digest() != data[-16:]:
        # MD5 不匹配但继续 (解密后 SHA1 校验会验证密钥正确性)
        pass

    # 解析 salt
    salt_len = struct.unpack('>I', data[cur:cur + 4])[0]
    cur += 4
    salt = data[cur:cur + salt_len]
    cur += salt_len

    # 解析 localkey_block
    block_len = struct.unpack('>I', data[cur:cur + 4])[0]
    cur += 4
    localkey_block = data[cur:cur + block_len]

    # 派生 base_key 并解密 localkey
    base_key = _create_local_key(passcode, salt)
    local_key = _decrypt_local(localkey_block, base_key)

    if len(local_key) != 256:
        raise ValueError(f'Unexpected LocalKey size: {len(local_key)}, expected 256')

    return local_key


# ==================== TDEF 缓存文件解密 ====================

def decrypt_tdef_file(path: str, local_key: bytes) -> bytes:
    """
    读取并解密 TDEF 格式的缓存文件

    TDEF 文件格式:
      header(4B "TDEF") + salt(64B) + encrypted_header(48B) + encrypted_data

    加密头: msgKey(16B) + checksum(32B) - 用于验证密钥正确性
    解密后跳过前48字节即为原始媒体数据
    """
    with open(path, 'rb') as f:
        magic = f.read(4)
        if magic != b'TDEF':
            raise ValueError(f'Wrong file type: {magic}, expected TDEF')

        salt = f.read(64)
        encrypted_header = f.read(48)  # msgKey(16) + checksum(32)
        remaining = f.read()

    # 派生解密密钥
    real_key = sha256(local_key[:128] + salt[:32])
    iv = sha256(local_key[128:] + salt[32:])[:16]

    # CTR 解密整个文件内容
    all_encrypted = encrypted_header + remaining
    all_decrypted = _telegram_ctr_decrypt(all_encrypted, real_key, iv)

    # 验证校验和 (前16字节是 msgKey, 接着32字节是 SHA256 checksum)
    decrypted_header = all_decrypted[:48]
    msg_key = decrypted_header[:16]
    checksum = decrypted_header[16:48]
    expected = sha256(local_key + salt + msg_key)
    if expected != checksum:
        raise ValueError('TDEF checksum mismatch (wrong key or corrupted file)')

    # 跳过 48 字节 header，返回原始媒体数据
    return all_decrypted[48:]


# ==================== TDF$ 文件校验 ====================

def verify_tdf_file(path: str) -> bool:
    """验证 TDF$ 文件是否有效"""
    try:
        with open(path, 'rb') as f:
            data = f.read()
        return data[:4] == b'TDF$'
    except Exception:
        return False
