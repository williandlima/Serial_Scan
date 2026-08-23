"""Serial Scan - analisador de barramentos seriais RS-232 / RS-485 / RS-422.

Fluxo do projeto:

1. escolher o protocolo (:mod:`serial_scan.protocols`);
2. identificar automaticamente a configuracao do frame
   (:mod:`serial_scan.autodetect`);
3. mostrar os dados do frame (:mod:`serial_scan.framing`);
4. identificar e segregar cada comando novo (:mod:`serial_scan.commands`);
5. rotular cada comando (:mod:`serial_scan.labels`).
"""

from .autodetect import (
    Candidate,
    DetectionReport,
    analyse_capture,
    analyse_samples,
    scan_candidates,
)
from .checksums import ALGORITHMS, ChecksumAlgorithm, detect_checksum
from .commands import CommandCatalog, CommandEntry, FieldKind, FieldStats
from .framing import Direction, Frame, FrameSplitter, TimedByte, split_frames
from .labels import LabelStore
from .portconfig import STANDARD_BAUDRATES, SerialConfig
from .protocols import PROFILES, Protocol, ProtocolProfile, get_profile
from .scoring import StreamScore, score_frames
from .session import ScanSession, session_from_capture, session_from_simulator
from .sources import (
    ByteSource,
    CaptureWriter,
    ReplaySource,
    SerialSource,
    SimulatedSource,
    list_ports,
)

__version__ = "0.1.0"

__all__ = [
    "ALGORITHMS",
    "PROFILES",
    "STANDARD_BAUDRATES",
    "ByteSource",
    "Candidate",
    "CaptureWriter",
    "ChecksumAlgorithm",
    "CommandCatalog",
    "CommandEntry",
    "DetectionReport",
    "Direction",
    "FieldKind",
    "FieldStats",
    "Frame",
    "FrameSplitter",
    "LabelStore",
    "Protocol",
    "ProtocolProfile",
    "ReplaySource",
    "ScanSession",
    "SerialConfig",
    "SerialSource",
    "SimulatedSource",
    "StreamScore",
    "TimedByte",
    "analyse_capture",
    "analyse_samples",
    "detect_checksum",
    "get_profile",
    "list_ports",
    "scan_candidates",
    "score_frames",
    "session_from_capture",
    "session_from_simulator",
    "split_frames",
    "__version__",
]
