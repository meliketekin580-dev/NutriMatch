"""NutriMatch günlük öğün kayıtlarını MongoDB Atlas'ta saklar.

Arayüzün kullandığı sözlük yapısı korunur. MongoDB içinde tarihler gerçek
``datetime`` değerleri olarak tutulur; arayüze dönerken tekrar ISO metnine
çevrilir. Eski ``data/nutrimatch.db`` dosyası değiştirilmez ve içindeki
kayıtlar güvenli, tekrarlanabilir bir migration ile MongoDB'ye aktarılır.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import streamlit as st

try:
    from pymongo import ASCENDING, DESCENDING, MongoClient
    from pymongo.collection import Collection
    from pymongo.errors import (
        ConfigurationError,
        DuplicateKeyError,
        OperationFailure,
        PyMongoError,
        ServerSelectionTimeoutError,
    )
except ImportError:  # Paket kurulmadan modülün saf tarih yardımcıları kullanılabilir.
    ASCENDING = 1
    DESCENDING = -1
    MongoClient = None  # type: ignore[assignment]
    Collection = Any  # type: ignore[misc,assignment]

    class PyMongoError(Exception):
        """PyMongo kurulu değilken güvenli hata yakalama türü sağlar."""

    class DuplicateKeyError(PyMongoError):
        """PyMongo kurulu değilken yinelenen anahtar hatasını temsil eder."""

    class ConfigurationError(PyMongoError):
        """PyMongo yapılandırma hatası için yedek türdür."""

    class OperationFailure(PyMongoError):
        """MongoDB işlem hatası için yedek türdür."""

    class ServerSelectionTimeoutError(PyMongoError):
        """MongoDB sunucu seçimi zaman aşımı için yedek türdür."""


# Eski SQLite dosyası migration kaynağıdır; silinmez veya değiştirilmez.
DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "nutrimatch.db"
COLLECTION_NAME = "daily_meals"
MIGRATION_COLLECTION_NAME = "nutrimatch_migrations"
SQLITE_MIGRATION_ID = "sqlite_daily_meals_v1"
LEGACY_USER_CLAIM_ID = "legacy_userless_daily_meals_claim_v1"
logger = logging.getLogger(__name__)


class DailyMealStoreError(RuntimeError):
    """Kalıcı günlük öğün deposu kullanılamadığında oluşur."""


def _mongo_failure_category(exc: BaseException) -> str:
    """MongoDB hatasını, gizli bağlantı bilgisini açığa çıkarmadan sınıflandırır."""
    message = str(exc).casefold()
    if isinstance(exc, ServerSelectionTimeoutError):
        if "ssl handshake" in message or "tls" in message:
            return "tls_handshake"
        if "resolution" in message or "dns" in message:
            return "dns_resolution"
        return "server_selection_timeout"
    if isinstance(exc, ConfigurationError):
        if "resolution" in message or "dns" in message:
            return "dns_resolution"
        return "configuration"
    if isinstance(exc, OperationFailure):
        if getattr(exc, "code", None) == 18:
            return "authentication"
        if getattr(exc, "code", None) == 13:
            return "authorization"
        if getattr(exc, "code", None) in {85, 86}:
            return "index_conflict"
        return "operation_failure"
    return "mongo_error"


def _public_mongo_error(category: str, fallback: str) -> str:
    """Teknik MongoDB hata kategorisini kullanıcıya uygun Türkçe mesaja çevirir."""
    messages = {
        "tls_handshake": (
            "MongoDB Atlas ile güvenli bağlantı kurulamadı. Atlas ağ erişim listesini, "
            "ağ/proxy ayarlarını ve bilgisayarın tarih-saatini kontrol edin."
        ),
        "dns_resolution": "MongoDB Atlas sunucu adı çözümlenemedi. İnternet ve DNS bağlantısını kontrol edin.",
        "server_selection_timeout": "MongoDB Atlas sunucusuna zamanında ulaşılamadı. Ağ erişimini kontrol edin.",
        "authentication": "MongoDB kullanıcı adı veya parolası kabul edilmedi.",
        "authorization": "MongoDB kullanıcısının günlük öğünler için okuma-yazma yetkisi bulunmuyor.",
        "index_conflict": "MongoDB günlük öğün koleksiyonunun indeks yapısı beklenen ayarlarla çakışıyor.",
        "configuration": "MongoDB bağlantı yapılandırması geçerli değil.",
    }
    return messages.get(category, fallback)


def _store_error(operation: str, fallback: str, exc: BaseException) -> DailyMealStoreError:
    """Güvenli log üretir ve arayüzde gösterilecek depo hatasını hazırlar."""
    category = _mongo_failure_category(exc)
    logger.error(
        "Günlük öğün MongoDB işlemi başarısız: operation=%s category=%s error_type=%s code=%s",
        operation,
        category,
        type(exc).__name__,
        getattr(exc, "code", None),
    )
    return DailyMealStoreError(_public_mongo_error(category, fallback))


def _mongo_settings() -> tuple[str, str]:
    """MongoDB bağlantı adresini ve veritabanı adını Streamlit secrets'tan alır."""
    try:
        uri = str(st.secrets["MONGODB_URI"]).strip()
        database_name = str(st.secrets.get("MONGODB_DATABASE", "nutrimatch")).strip()
    except (KeyError, FileNotFoundError, TypeError):
        raise DailyMealStoreError("Günlük öğün veritabanı bağlantısı yapılandırılmamış.") from None
    if not uri:
        raise DailyMealStoreError("Günlük öğün veritabanı bağlantısı yapılandırılmamış.")
    return uri, database_name or "nutrimatch"


@st.cache_resource(show_spinner=False)
def _mongo_client(uri: str) -> Any:
    """Uygulama oturumlarında tekrar kullanılacak MongoClient nesnesini oluşturur."""
    if MongoClient is None:
        raise DailyMealStoreError("MongoDB desteği kurulu değil. PyMongo paketini yükleyin.")
    return MongoClient(
        uri,
        tz_aware=True,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=5000,
    )


def _database() -> Any:
    """Yapılandırılmış MongoDB veritabanını döndürür."""
    uri, database_name = _mongo_settings()
    return _mongo_client(uri)[database_name]


def _collection() -> Collection:
    """Günlük öğünlerin tutulduğu MongoDB koleksiyonunu döndürür."""
    return _database()[COLLECTION_NAME]


def _current_user_id() -> str:
    """Giriş yapan kullanıcının OIDC ``sub`` değerini güvenli biçimde döndürür.

    Kimlik doğrulaması test veya bakım ortamında yoksa boş metin döndürülür;
    böylece mevcut servis fonksiyonlarının imzaları değişmeden kalır.
    """
    try:
        if not bool(st.user.is_logged_in):
            return ""
        return str(st.user.get("sub") or "").strip()
    except (AttributeError, KeyError, TypeError):
        return ""


def _user_filter() -> dict[str, Any]:
    """MongoDB sorgusunu varsa giriş yapan kullanıcının kayıtlarıyla sınırlar."""
    user_id = _current_user_id()
    return {"user_id": user_id} if user_id else {}


def _parse_datetime(value: object) -> datetime:
    """ISO metni veya datetime değerini MongoDB için UTC datetime'a dönüştürür."""
    if isinstance(value, datetime):
        parsed = value
    else:
        raw_value = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now().astimezone()
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def _record_to_document(record: dict[str, Any]) -> dict[str, Any]:
    """Arayüzde kullanılan öğün sözlüğünü MongoDB belgesine dönüştürür."""
    return {
        "analysis_id": str(record.get("analysis_id") or ""),
        "meal_name": str(record.get("meal_name") or "Analiz edilen öğün"),
        "created_at": _parse_datetime(record.get("datetime")),
        "goal": str(record.get("goal") or "Dengeli Beslenme"),
        "items": record.get("items") if isinstance(record.get("items"), list) else [],
        "total_calories_kcal": record.get("total_calories_kcal"),
        "total_protein_g": record.get("total_protein_g"),
        "total_carbohydrates_g": record.get("total_carbohydrates_g"),
        "total_fat_g": record.get("total_fat_g"),
        "total_fiber_g": record.get("total_fiber_g"),
        "image_hash": str(record.get("image_hash") or ""),
    }


def _document_to_record(document: dict[str, Any]) -> dict[str, Any]:
    """MongoDB belgesini mevcut arayüzün beklediği öğün sözlüğüne dönüştürür."""
    created_at = document.get("created_at")
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        datetime_value = created_at.astimezone().isoformat(timespec="seconds")
    else:
        datetime_value = str(created_at or "")
    return {
        "analysis_id": str(document.get("analysis_id") or ""),
        "meal_name": str(document.get("meal_name") or "Analiz edilen öğün"),
        "datetime": datetime_value,
        "goal": str(document.get("goal") or "Dengeli Beslenme"),
        "items": document.get("items") if isinstance(document.get("items"), list) else [],
        "total_calories_kcal": document.get("total_calories_kcal"),
        "total_protein_g": document.get("total_protein_g"),
        "total_carbohydrates_g": document.get("total_carbohydrates_g"),
        "total_fat_g": document.get("total_fat_g"),
        "total_fiber_g": document.get("total_fiber_g"),
        "image_hash": str(document.get("image_hash") or ""),
    }


def _sqlite_records() -> list[dict[str, Any]]:
    """Mevcut SQLite öğünlerini dosyayı değiştirmeden salt okunur olarak okur."""
    if not DATABASE_PATH.is_file():
        return []
    try:
        database_uri = DATABASE_PATH.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("SELECT * FROM daily_meals ORDER BY created_at ASC").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        logger.error(
            "SQLite migration kaynağı okunamadı: error_type=%s",
            type(exc).__name__,
        )
        raise DailyMealStoreError("Eski günlük öğün kayıtları aktarım için okunamadı.") from exc

    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            items = json.loads(row["items_json"])
        except (TypeError, ValueError):
            items = []
        records.append(
            {
                "analysis_id": row["analysis_id"],
                "meal_name": row["meal_name"],
                "datetime": row["created_at"],
                "goal": row["goal"],
                "items": items if isinstance(items, list) else [],
                "total_calories_kcal": row["total_calories_kcal"],
                "total_protein_g": row["total_protein_g"],
                "total_carbohydrates_g": row["total_carbohydrates_g"],
                "total_fat_g": row["total_fat_g"],
                "total_fiber_g": row["total_fiber_g"],
                "image_hash": row["image_hash"],
            }
        )
    return records


def _migrate_sqlite_records_once(collection: Collection) -> int:
    """SQLite kayıtlarını MongoDB'ye yalnızca bir kez ve tekrarsız aktarır."""
    migrations = collection.database[MIGRATION_COLLECTION_NAME]
    if migrations.find_one({"_id": SQLITE_MIGRATION_ID}, {"_id": 1}):
        return 0

    migrated_count = 0
    for record in _sqlite_records():
        document = _record_to_document(record)
        analysis_id = document["analysis_id"]
        if not analysis_id:
            continue
        result = collection.update_one(
            {"analysis_id": analysis_id},
            {"$setOnInsert": document},
            upsert=True,
        )
        if result.upserted_id is not None:
            migrated_count += 1

    # İşaret yalnızca bütün kayıtlar sorunsuz işlendiğinde yazılır.
    migrations.update_one(
        {"_id": SQLITE_MIGRATION_ID},
        {"$set": {"completed_at": datetime.now(timezone.utc), "migrated_count": migrated_count}},
        upsert=True,
    )
    return migrated_count


def migrate_sqlite_daily_meals_once() -> int:
    """Eski SQLite öğünlerini MongoDB'ye güvenli ve tekrarsız şekilde aktarır."""
    try:
        collection = _collection()
        collection.create_index([("analysis_id", ASCENDING)], unique=True, name="analysis_id_unique")
        collection.create_index([("created_at", DESCENDING)], name="created_at_desc")
        return _migrate_sqlite_records_once(collection)
    except (PyMongoError, DailyMealStoreError) as exc:
        if isinstance(exc, DailyMealStoreError):
            raise
        raise _store_error(
            "sqlite_migration",
            "Eski günlük öğün kayıtları MongoDB'ye aktarılamadı.",
            exc,
        ) from None


@st.cache_resource(show_spinner=False)
def _prepare_daily_meal_store(uri: str, database_name: str) -> bool:
    """Bağlantıyı doğrular, indeksleri ve migration'ı uygulama sürecinde bir kez hazırlar."""
    client = _mongo_client(uri)
    client.admin.command("ping")
    collection = client[database_name][COLLECTION_NAME]
    collection.create_index([("analysis_id", ASCENDING)], unique=True, name="analysis_id_unique")
    collection.create_index([("created_at", DESCENDING)], name="created_at_desc")
    _migrate_sqlite_records_once(collection)
    return True


def initialize_daily_meal_store() -> None:
    """MongoDB koleksiyonunu, indeksleri ve tek seferlik migration'ı hazırlar."""
    try:
        uri, database_name = _mongo_settings()
        _prepare_daily_meal_store(uri, database_name)
    except (PyMongoError, DailyMealStoreError) as exc:
        if isinstance(exc, DailyMealStoreError):
            raise
        raise _store_error(
            "initialize",
            "Günlük öğün veritabanı şu anda hazırlanamadı.",
            exc,
        ) from None


def save_daily_meal(record: dict[str, Any]) -> bool:
    """Öğün kaydını MongoDB'ye ekler; aynı analiz kimliğini tekrar eklemez."""
    initialize_daily_meal_store()
    document = _record_to_document(record)
    user_id = _current_user_id()
    if user_id:
        document["user_id"] = user_id
    if not document["analysis_id"]:
        raise DailyMealStoreError("Öğün kaydı için geçerli bir analiz kimliği bulunamadı.")
    try:
        result = _collection().update_one(
            {"analysis_id": document["analysis_id"], **_user_filter()},
            {"$setOnInsert": document},
            upsert=True,
        )
        return result.upserted_id is not None
    except PyMongoError as exc:
        raise _store_error("insert", "Öğün günlüğe kaydedilemedi.", exc) from None


def load_daily_meals() -> list[dict[str, Any]]:
    """MongoDB'deki bütün öğünleri en yeni kayıt önce olacak şekilde listeler."""
    initialize_daily_meal_store()
    try:
        documents = _collection().find(_user_filter(), {"_id": 0}).sort("created_at", DESCENDING)
        return [_document_to_record(document) for document in documents]
    except PyMongoError as exc:
        raise _store_error("read_all", "Günlük öğünler okunamadı.", exc) from None


def _date_bounds(selected_date: date) -> tuple[datetime, datetime]:
    """Seçili yerel gün için MongoDB sorgusunda kullanılacak UTC sınırlarını üretir."""
    local_timezone = datetime.now().astimezone().tzinfo or timezone.utc
    start_local = datetime.combine(selected_date, time.min, tzinfo=local_timezone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def load_daily_meals_for_date(selected_date: date) -> list[dict[str, Any]]:
    """Yalnızca seçilen yerel güne ait öğünleri MongoDB'den getirir."""
    initialize_daily_meal_store()
    start_utc, end_utc = _date_bounds(selected_date)
    try:
        documents = (
            _collection()
            .find(
                {"created_at": {"$gte": start_utc, "$lt": end_utc}, **_user_filter()},
                {"_id": 0},
            )
            .sort("created_at", DESCENDING)
        )
        return [_document_to_record(document) for document in documents]
    except PyMongoError as exc:
        raise _store_error("read_by_date", "Seçilen güne ait öğünler okunamadı.", exc) from None


def update_daily_meal(analysis_id: str, updates: dict[str, Any]) -> bool:
    """Bir öğünün desteklenen alanlarını MongoDB içinde günceller."""
    initialize_daily_meal_store()
    allowed_fields = {
        "meal_name", "goal", "items", "total_calories_kcal", "total_protein_g",
        "total_carbohydrates_g", "total_fat_g", "total_fiber_g", "image_hash",
    }
    mongo_updates = {key: value for key, value in updates.items() if key in allowed_fields}
    if "datetime" in updates:
        mongo_updates["created_at"] = _parse_datetime(updates.get("datetime"))
    if not mongo_updates:
        return False
    try:
        result = _collection().update_one(
            {"analysis_id": str(analysis_id), **_user_filter()},
            {"$set": mongo_updates},
        )
        return result.matched_count == 1
    except PyMongoError as exc:
        raise _store_error("update", "Öğün kaydı güncellenemedi.", exc) from None


def delete_daily_meal(analysis_id: str) -> bool:
    """Analiz kimliği verilen öğünü MongoDB'den siler."""
    initialize_daily_meal_store()
    try:
        result = _collection().delete_one({"analysis_id": str(analysis_id), **_user_filter()})
        return result.deleted_count == 1
    except PyMongoError as exc:
        raise _store_error("delete", "Öğün günlükten silinemedi.", exc) from None


def count_claimable_legacy_daily_meals(user_id: str) -> int:
    """Mevcut hesabın açıkça sahiplenebileceği sahipsiz eski öğünleri sayar.

    Yalnızca ``user_id`` alanı hiç bulunmayan belgeler sayılır. Geçiş başka bir
    hesap tarafından başlatılmış veya tamamlanmışsa bu hesaba kayıt gösterilmez.
    """
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        return 0
    initialize_daily_meal_store()
    try:
        collection = _collection()
        migrations = collection.database[MIGRATION_COLLECTION_NAME]
        marker = migrations.find_one({"_id": LEGACY_USER_CLAIM_ID})
        if marker and str(marker.get("claimed_by") or "") != normalized_user_id:
            return 0
        return int(collection.count_documents({"user_id": {"$exists": False}}))
    except PyMongoError as exc:
        raise _store_error(
            "legacy_claim_count",
            "Eski öğün kayıtları şu anda kontrol edilemedi.",
            exc,
        ) from None


def claim_legacy_daily_meals_for_user(user_id: str) -> int:
    """Sahipsiz eski öğünleri açık kullanıcı onayıyla tek hesaba bağlar.

    Küresel geçiş kilidi ilk aktarımı yapan hesabı kaydeder. Aynı hesap işlemi
    güvenle yeniden çalıştırabilir; başka hesap bu kayıtları alamaz. ``user_id``
    alanı bulunan hiçbir belge güncellenmez.
    """
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        raise DailyMealStoreError("Eski öğünleri aktarmak için giriş yapan kullanıcı bilgisi bulunamadı.")
    initialize_daily_meal_store()
    try:
        collection = _collection()
        migrations = collection.database[MIGRATION_COLLECTION_NAME]
        marker = migrations.find_one({"_id": LEGACY_USER_CLAIM_ID})
        if marker is None:
            try:
                migrations.insert_one(
                    {
                        "_id": LEGACY_USER_CLAIM_ID,
                        "claimed_by": normalized_user_id,
                        "status": "pending",
                        "started_at": datetime.now(timezone.utc),
                    }
                )
            except DuplicateKeyError:
                marker = migrations.find_one({"_id": LEGACY_USER_CLAIM_ID})
        if marker is None:
            marker = migrations.find_one({"_id": LEGACY_USER_CLAIM_ID})
        if marker and str(marker.get("claimed_by") or "") != normalized_user_id:
            return 0

        result = collection.update_many(
            {"user_id": {"$exists": False}},
            {
                "$set": {
                    "user_id": normalized_user_id,
                    "legacy_claimed_at": datetime.now(timezone.utc),
                }
            },
        )
        migrations.update_one(
            {"_id": LEGACY_USER_CLAIM_ID, "claimed_by": normalized_user_id},
            {
                "$set": {
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc),
                    "claimed_count": int(result.modified_count),
                }
            },
        )
        return int(result.modified_count)
    except PyMongoError as exc:
        raise _store_error(
            "legacy_claim",
            "Eski öğün kayıtları hesabınıza aktarılamadı.",
            exc,
        ) from None


def local_meal_date(record: dict[str, Any]) -> date | None:
    """Öğün kaydının tarihini uygulamanın yerel saat dilimindeki güne dönüştürür."""
    raw_datetime = record.get("datetime")
    if isinstance(raw_datetime, datetime):
        parsed = raw_datetime
    else:
        raw_text = str(raw_datetime or "").strip()
        if not raw_text:
            return None
        try:
            parsed = datetime.fromisoformat(raw_text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().date()


def meals_for_date(meals: list[dict[str, Any]], selected_date: date) -> list[dict[str, Any]]:
    """Mevcut arayüz uyumluluğu için listedeki öğünleri seçili yerel güne göre süzer."""
    return [meal for meal in meals if local_meal_date(meal) == selected_date]


def weekly_summary(meals: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
    """Son yedi günün toplamlarını, ortalamalarını ve öğün sayısını hesaplar."""
    end_day = today or datetime.now().date()
    days = [end_day - timedelta(days=offset) for offset in range(6, -1, -1)]
    daily = {
        day.isoformat(): {
            "date": day.isoformat(), "label": day.strftime("%d %b"),
            "calories_kcal": 0.0, "protein_g": 0.0, "carbohydrates_g": 0.0,
            "fat_g": 0.0, "meal_count": 0,
        }
        for day in days
    }
    numeric_fields = ("calories_kcal", "protein_g", "carbohydrates_g", "fat_g")
    for meal in meals:
        meal_date = local_meal_date(meal)
        if meal_date is None:
            continue
        meal_day = meal_date.isoformat()
        if meal_day not in daily:
            continue
        daily[meal_day]["meal_count"] += 1
        for field in numeric_fields:
            source_key = f"total_{field}"
            try:
                daily[meal_day][field] += float(meal.get(source_key) or 0)
            except (TypeError, ValueError):
                continue
    rows = list(daily.values())
    totals = {field: round(sum(row[field] for row in rows), 1) for field in numeric_fields}
    averages = {field: round(totals[field] / len(rows), 1) for field in numeric_fields}
    return {
        "days": rows,
        "totals": totals,
        "averages": averages,
        "meal_count": sum(row["meal_count"] for row in rows),
    }
