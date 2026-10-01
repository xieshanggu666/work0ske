"""API 冒烟测试：登录、鉴权与仪表盘。"""

import pytest
from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, engine
from app.core.security import hash_password
from app.main import app
from app.models import Company, User


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    pwd_hash, salt = hash_password("123456")
    if not db.query(User).filter(User.username == "admin").first():
        db.add(User(username="admin", display_name="监管管理员", role="admin", password_hash=pwd_hash, salt=salt))
    if not db.query(Company).filter(Company.code == "API-001").first():
        db.add(Company(code="API-001", name="接口测试企业", industry="电力", region="测试"))
    db.commit()
    db.close()
    return TestClient(app)


def test_login_success(client):
    res = client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    assert res.status_code == 200
    assert res.json()["user"]["role"] == "admin"


def test_login_wrong_password(client):
    res = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
    assert res.status_code == 401


def test_me_requires_auth(client):
    client.cookies.clear()
    res = client.get("/api/auth/me")
    assert res.status_code == 401


def test_dashboard_stats(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    res = client.get("/api/dashboard/stats")
    assert res.status_code == 200
    data = res.json()
    assert data["total_companies"] >= 1
    assert "emission_total" in data
    assert "quota_total" in data


def test_calculate_skips_unverified_activity(client):
    """未核验活动数据不进入核算与年度汇总；核验后重算即纳入。"""
    from app.models import EmissionFactor

    client.post("/api/auth/login", json={"username": "admin", "password": "123456"})

    company = client.post("/api/companies", json={
        "code": "API-CALC-001", "name": "核算门禁测试企业", "industry": "电力", "region": "测试",
    }).json()
    cid = company["id"]
    scope = client.post(f"/api/companies/{cid}/scopes", json={
        "scope": "2", "category": "外购电力", "name": "厂区用电",
    }).json()
    # 直接落库因子（POST /api/factors 存在与本次修复无关的既有缺陷，见 FactorVersion 未 flush）
    db = SessionLocal()
    db.add(EmissionFactor(
        factor_code="API-ELEC-2026", name="外购电力", scope="2", unit="tCO2/MWh",
        value=0.5703, source="测试因子", valid_from="2026-01-01", valid_to="2026-12-31",
    ))
    db.commit()
    db.close()

    pending = client.post("/api/activity", json={
        "scope_id": scope["id"], "year": 2026, "period": "monthly",
        "activity_type": "外购电力", "unit": "MWh", "quantity": 1000, "data_source": "台账",
    }).json()
    verified = client.post("/api/activity", json={
        "scope_id": scope["id"], "year": 2026, "period": "monthly",
        "activity_type": "外购电力", "unit": "MWh", "quantity": 2000, "data_source": "台账",
    }).json()
    assert client.post(f"/api/activity/{verified['id']}/verify").status_code == 200

    body = client.post(f"/api/companies/{cid}/calculate?year=2026").json()
    assert body["count"] == 1
    assert body["skipped_unverified"] == 1
    assert body["total"] == pytest.approx(2000 * 0.5703, rel=1e-6)

    totals = client.get(f"/api/companies/{cid}/totals?year=2026").json()
    assert totals["total"] == pytest.approx(2000 * 0.5703, rel=1e-6)

    # 核验后重新核算即纳入，且先清后算保持幂等
    assert client.post(f"/api/activity/{pending['id']}/verify").status_code == 200
    body = client.post(f"/api/companies/{cid}/calculate?year=2026").json()
    assert body["count"] == 2
    assert body["skipped_unverified"] == 0
    assert body["total"] == pytest.approx(3000 * 0.5703, rel=1e-6)
