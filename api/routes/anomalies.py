from fastapi import APIRouter


router = APIRouter(tags=["anomalies"])


@router.get("/hypotheses/{hypothesis_id}")
def get_hypothesis(hypothesis_id: str) -> dict:
    return {"hypothesis_id": hypothesis_id, "status": "not_implemented_yet"}


@router.get("/anomalies/{anomaly_id}")
def get_anomaly(anomaly_id: str) -> dict:
    return {"anomaly_id": anomaly_id, "status": "not_implemented_yet"}
