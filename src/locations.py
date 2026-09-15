"""
Telegram Desktop locations + downloads 解析模块

解析 tdata 中的 locations 文件 (TDF$ 格式), 提取:
1. MediaKey → FileLocation 映射 (locations)
   - document_id → 文件路径 / *media_cache* 标记
2. downloadsSerialized → {documentId, peerId, msgId, path} 列表

用于支持:
- 从缓存文件关联到 Telegram 消息 (通过 document_id)
- 生成 tg:// 跳转 URL (通过 peerId + msgId)

locations 文件格式 (readLocations):
  while loop:
    quint64 first  (MediaKey.first = (type << 32) | dc)
    quint64 second (MediaKey.second = document_id)
    quint32 legacyType
    QString fname (*media_cache* 表示缓存在 media_cache)
    QByteArray bookmark
    QDateTime modified (13B)
    quint32 size
  [结束标记: first=0, second=0]
  quint32 aliasesCount + aliases (each 32B)
  quint32 webLocationsCount + webLocations
  QByteArray downloadsSerialized:
    qint32 count
    for each:
      quint64 objectId (documentId)
      qint32  type (0=Document, 1=Photo)
      qint64  started
      quint32 size
      quint64 peerId
      qint64  msgId
      quint64 peerAccessHash
      QString path

PeerId 编码 (data_peer_id.h):
  PeerId = bareId | (typeShift << 48)
  typeShift: 0=User, 1=Chat, 2=Channel, 3=Megagroup
"""

import os
import struct
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from .crypto import extract_local_key, _decrypt_local


# LocationType 枚举
LOCATION_TYPES = {
    0x4E45ABE9: 'DocumentFileLocation',
    0x74DC404D: 'AudioFileLocation',
    0x3D0364EC: 'VideoFileLocation',
    0xCBC7EE28: 'SecureFileLocation',
}


@dataclass
class FileLocation:
    """locations 文件中的一条记录"""
    media_key_first: int    # (type << 32) | dc_id
    media_key_second: int   # document_id
    dc_id: int
    location_type: str
    document_id: int
    fname: Optional[str]     # 文件路径; "*media_cache*" 表示缓存在 media_cache
    file_size: int

    @property
    def is_media_cache(self) -> bool:
        """是否缓存在 media_cache (而非已下载到本地)"""
        return self.fname == '*media_cache*'

    @property
    def is_downloaded(self) -> bool:
        """是否已下载到本地"""
        return self.fname is not None and self.fname != '*media_cache*'


@dataclass
class DownloadRecord:
    """downloadsSerialized 中的一条下载记录"""
    document_id: int
    download_type: int       # 0=Document, 1=Photo
    started: int             # 下载开始时间 (epoch ms)
    size: int
    peer_id: int
    msg_id: int
    peer_access_hash: int
    path: Optional[str]
    peer_type: str           # User, Chat, Channel, Megagroup
    bare_id: int

    @property
    def basename(self) -> str:
        if self.path:
            return os.path.basename(self.path)
        return ''

    def tg_url(self) -> Optional[str]:
        """生成 tg:// 跳转 URL
        Telegram Desktop 支持的格式:
        - Channel/Megagroup: tg://privatepost?channel=<bare_id>&post=<msg_id>
        - Chat (普通群组): tg://privatepost?chat=<bare_id>&post=<msg_id>
        - User 私聊: 无直接跳转 URL (Telegram Desktop 不支持)
        """
        if self.peer_type in ('Channel', 'Megagroup'):
            return f'tg://privatepost?channel={self.bare_id}&post={self.msg_id}'
        elif self.peer_type == 'Chat':
            return f'tg://privatepost?chat={self.bare_id}&post={self.msg_id}'
        return None


def _read_qstring(data: bytes, offset: int) -> Tuple[Optional[str], int]:
    """读取 QDataStream QString (BE, Qt_5_1)"""
    if offset + 4 > len(data):
        return None, offset
    byte_count = struct.unpack('>I', data[offset:offset + 4])[0]
    offset += 4
    if byte_count == 0xFFFFFFFF:
        return None, offset
    if byte_count == 0:
        return '', offset
    if offset + byte_count > len(data):
        return None, offset
    s = data[offset:offset + byte_count].decode('utf-16-be', errors='replace')
    return s, offset + byte_count


def _read_qbytearray(data: bytes, offset: int) -> Tuple[Optional[bytes], int]:
    """读取 QDataStream QByteArray (BE, Qt_5_1)"""
    if offset + 4 > len(data):
        return None, offset
    size = struct.unpack('>i', data[offset:offset + 4])[0]
    offset += 4
    if size == -1:
        return None, offset
    if size == 0:
        return b'', offset
    if offset + size > len(data):
        return None, offset
    return data[offset:offset + size], offset + size


def _read_qdatetime(data: bytes, offset: int) -> Tuple[int, int]:
    """读取 QDataStream QDateTime (13B = 8B msecs + 4B offset + 1B timespec)"""
    if offset + 13 > len(data):
        return 0, offset
    msecs = struct.unpack('>q', data[offset:offset + 8])[0]
    return msecs, offset + 13


def _decode_peer_id(peer_id_value: int) -> Tuple[str, int]:
    """
    解码 PeerId → (peer_type, bare_id)
    PeerId = bareId | (typeShift << 48)
    typeShift: 0=User, 1=Chat, 2=Channel, 3=Megagroup
    """
    type_shift = (peer_id_value >> 48) & 0xFF
    bare_id = peer_id_value & 0xFFFFFFFFFFFF
    type_name = {0: 'User', 1: 'Chat', 2: 'Channel', 3: 'Megagroup'}.get(
        type_shift, f'Unknown({type_shift})'
    )
    return type_name, bare_id


def _find_locations_file(tdata_path: str, local_key: bytes) -> Optional[str]:
    """
    在 tdata 中查找 locations 文件
    方法: 逐一解密 TDF$ 文件, 检查解密后第一个 MediaKey 的高32位是否匹配已知 LocationType
    """
    user_dir = os.path.join(tdata_path, 'D877F783D5D3EF8C')
    if not os.path.isdir(user_dir):
        return None

    # 遍历用户目录下的 TDF$ 文件
    for fname in os.listdir(user_dir):
        if fname in ('maps', 'configs', 'settingss'):
            continue
        fpath = os.path.join(user_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, 'rb') as f:
                data = f.read(12)
            if data[:4] != b'TDF$':
                continue
            data_len = struct.unpack('>I', data[8:12])[0]
            with open(fpath, 'rb') as f:
                full_data = f.read()
            if 12 + data_len > len(full_data):
                continue
            encrypted_block = full_data[12:12 + data_len]
            decrypted = _decrypt_local(encrypted_block, local_key)
            # 检查前8字节的高32位是否匹配已知 LocationType
            if len(decrypted) >= 8:
                first_high32 = (struct.unpack('>Q', decrypted[:8])[0] >> 32) & 0xFFFFFFFF
                if first_high32 in LOCATION_TYPES:
                    return fpath
        except Exception:
            continue
    return None


def parse_locations(tdata_path: str, local_key: bytes) -> Tuple[List[FileLocation], List[DownloadRecord]]:
    """
    解析 locations 文件, 返回 (locations, downloads)

    Args:
        tdata_path: tdata 目录路径
        local_key: LocalKey (256 字节)
    Returns:
        (FileLocation 列表, DownloadRecord 列表)
    """
    # 查找 locations 文件
    loc_file = _find_locations_file(tdata_path, local_key)
    if loc_file is None:
        return [], []

    with open(loc_file, 'rb') as f:
        data = f.read()

    if data[:4] != b'TDF$':
        return [], []

    data_len = struct.unpack('>I', data[8:12])[0]
    encrypted_block = data[12:12 + data_len]
    decrypted = _decrypt_local(encrypted_block, local_key)

    # === 解析 locations 记录 ===
    offset = 0
    locations = []
    while offset + 20 <= len(decrypted):
        first = struct.unpack('>Q', decrypted[offset:offset + 8])[0]
        second = struct.unpack('>Q', decrypted[offset + 8:offset + 16])[0]
        offset += 16

        if first == 0 and second == 0:
            # 结束标记
            offset += 4  # legacyType
            _, offset = _read_qstring(decrypted, offset)
            _, offset = _read_qbytearray(decrypted, offset)
            _, offset = _read_qdatetime(decrypted, offset)
            offset += 4  # size
            break

        legacy_type = struct.unpack('>I', decrypted[offset:offset + 4])[0]
        offset += 4
        fname, offset = _read_qstring(decrypted, offset)
        _, offset = _read_qbytearray(decrypted, offset)
        _, offset = _read_qdatetime(decrypted, offset)
        file_size = struct.unpack('>I', decrypted[offset:offset + 4])[0]
        offset += 4

        high32 = (first >> 32) & 0xFFFFFFFF
        dc_id = first & 0xFFFFFFFF
        loc_type = LOCATION_TYPES.get(high32, f'Unknown(0x{high32:08X})')

        locations.append(FileLocation(
            media_key_first=first,
            media_key_second=second,
            dc_id=dc_id,
            location_type=loc_type,
            document_id=second,
            fname=fname,
            file_size=file_size,
        ))

    # === 跳过 aliases ===
    if offset + 4 <= len(decrypted):
        aliases_count = struct.unpack('>I', decrypted[offset:offset + 4])[0]
        offset += 4
        offset += aliases_count * 32

    # === 跳过 webLocations ===
    if offset + 4 <= len(decrypted):
        web_count = struct.unpack('>I', decrypted[offset:offset + 4])[0]
        offset += 4
        # webLocations 格式不确定, 通常为 0
        # 如果 count > 0, 跳过会出错, 但实际数据中通常为 0

    # === 解析 downloadsSerialized ===
    downloads = []
    if offset + 4 <= len(decrypted):
        dl_size = struct.unpack('>i', decrypted[offset:offset + 4])[0]
        offset += 4

        if dl_size > 0 and offset + dl_size <= len(decrypted):
            dl_data = decrypted[offset:offset + dl_size]

            dl_offset = 0
            count = struct.unpack('>i', dl_data[dl_offset:dl_offset + 4])[0]
            dl_offset += 4

            if 0 < count < 100000:
                for _ in range(count):
                    if dl_offset + 8 + 4 + 8 + 4 + 8 + 8 + 8 > len(dl_data):
                        break

                    objectId = struct.unpack('>Q', dl_data[dl_offset:dl_offset + 8])[0]
                    dl_offset += 8
                    dl_type = struct.unpack('>i', dl_data[dl_offset:dl_offset + 4])[0]
                    dl_offset += 4
                    started = struct.unpack('>q', dl_data[dl_offset:dl_offset + 8])[0]
                    dl_offset += 8
                    size = struct.unpack('>I', dl_data[dl_offset:dl_offset + 4])[0]
                    dl_offset += 4
                    peerId = struct.unpack('>Q', dl_data[dl_offset:dl_offset + 8])[0]
                    dl_offset += 8
                    msgId = struct.unpack('>q', dl_data[dl_offset:dl_offset + 8])[0]
                    dl_offset += 8
                    peerAccessHash = struct.unpack('>Q', dl_data[dl_offset:dl_offset + 8])[0]
                    dl_offset += 8
                    dl_path, dl_offset = _read_qstring(dl_data, dl_offset)

                    peer_type, bare_id = _decode_peer_id(peerId)

                    downloads.append(DownloadRecord(
                        document_id=objectId,
                        download_type=dl_type,
                        started=started,
                        size=size,
                        peer_id=peerId,
                        msg_id=msgId,
                        peer_access_hash=peerAccessHash,
                        path=dl_path,
                        peer_type=peer_type,
                        bare_id=bare_id,
                    ))

    return locations, downloads


def build_document_index(
    locations: List[FileLocation],
    downloads: List[DownloadRecord]
) -> Dict[int, dict]:
    """
    构建 document_id → {location, download} 的索引

    Returns:
        {document_id: {
            'location': FileLocation,
            'download': DownloadRecord or None,
            'tg_url': str or None,
        }}
    """
    download_by_doc = {d.document_id: d for d in downloads}
    index = {}
    for loc in locations:
        d = download_by_doc.get(loc.document_id)
        index[loc.document_id] = {
            'location': loc,
            'download': d,
            'tg_url': d.tg_url() if d else None,
        }
    # 也添加只存在于 downloads 中但不在 locations 中的条目
    for doc_id, d in download_by_doc.items():
        if doc_id not in index:
            index[doc_id] = {
                'location': None,
                'download': d,
                'tg_url': d.tg_url(),
            }
    return index


def build_filename_index(downloads: List[DownloadRecord]) -> Dict[str, DownloadRecord]:
    """
    构建文件名 → DownloadRecord 索引 (用于按文件名匹配)

    Returns:
        {basename: DownloadRecord}
    """
    return {d.basename: d for d in downloads if d.basename}


class LocationIndex:
    """locations + downloads 的综合索引, 供 server.py 使用"""

    def __init__(self, tdata_path: str, local_key: bytes):
        self.locations: List[FileLocation] = []
        self.downloads: List[DownloadRecord] = []
        self.doc_index: Dict[int, dict] = {}
        self.filename_index: Dict[str, DownloadRecord] = {}
        self._loaded = False
        self._tdata_path = tdata_path
        self._local_key = local_key

    def load(self):
        """加载并解析 locations 文件"""
        try:
            self.locations, self.downloads = parse_locations(
                self._tdata_path, self._local_key
            )
            self.doc_index = build_document_index(self.locations, self.downloads)
            self.filename_index = build_filename_index(self.downloads)
            self._loaded = True
        except Exception as e:
            self._loaded = False
            raise

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def get_by_document_id(self, doc_id: int) -> Optional[dict]:
        """通过 document_id 查找"""
        return self.doc_index.get(doc_id)

    def get_download_by_filename(self, filename: str) -> Optional[DownloadRecord]:
        """通过文件名查找下载记录"""
        return self.filename_index.get(filename)
