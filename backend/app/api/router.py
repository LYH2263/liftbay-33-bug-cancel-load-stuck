from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Building, CallTicket, DispatchLog, ElevatorCar
from app.schemas.schemas import (
    BuildingOut,
    CallCreate,
    CallOut,
    CarOut,
    CongestionFloor,
    DispatchRequest,
    LogOut,
)
from app.services.dispatch_engine import CallRequest, CarState, congestion_by_floor, pick_car

api_router = APIRouter()


@api_router.get("/health")
def health():
    return {"status": "ok"}


@api_router.get("/buildings", response_model=list[BuildingOut])
def buildings(db: Session = Depends(get_db)):
    return db.scalars(select(Building).order_by(Building.id)).all()


@api_router.get("/cars", response_model=list[CarOut])
def cars(db: Session = Depends(get_db)):
    return db.scalars(select(ElevatorCar).order_by(ElevatorCar.id)).all()


@api_router.get("/calls", response_model=list[CallOut])
def calls(db: Session = Depends(get_db)):
    return db.scalars(select(CallTicket).order_by(CallTicket.id.desc())).all()


@api_router.post("/calls", response_model=CallOut)
def create_call(body: CallCreate, db: Session = Depends(get_db)):
    b = db.get(Building, body.building_id)
    if not b:
        raise HTTPException(404, "楼栋不存在")
    if body.floor > b.floors:
        raise HTTPException(400, "楼层超出")
    if body.direction not in ("up", "down"):
        raise HTTPException(400, "方向无效")
    ticket = CallTicket(
        building_id=body.building_id,
        floor=body.floor,
        direction=body.direction,
        passengers=body.passengers,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return ticket


@api_router.post("/dispatch", response_model=CallOut)
def dispatch(body: DispatchRequest, db: Session = Depends(get_db)):
    ticket = db.get(CallTicket, body.call_id)
    if not ticket:
        raise HTTPException(404, "呼梯不存在")
    if ticket.status == "cancelled":
        raise HTTPException(400, "呼梯已取消，不可再派工")
    if ticket.status == "rejected":
        raise HTTPException(400, "呼梯已拒派，不可再派工")
    if ticket.status == "assigned":
        raise HTTPException(400, "呼梯已派工，不可重复派工")
    if ticket.status != "waiting":
        raise HTTPException(400, "呼梯已处理")
    car_rows = db.scalars(
        select(ElevatorCar).where(ElevatorCar.building_id == ticket.building_id)
    ).all()
    cars = [
        CarState(c.id, c.floor, c.direction, c.load, c.capacity) for c in car_rows
    ]
    call = CallRequest(ticket.id, ticket.floor, ticket.direction, ticket.passengers)
    best = pick_car(cars, call)
    if best is None:
        db.add(DispatchLog(call_id=ticket.id, car_id=None, detail="全部轿厢满员，拒绝派工"))
        ticket.status = "rejected"
        db.commit()
        db.refresh(ticket)
        raise HTTPException(409, "无可用轿厢（满员）")
    car = db.get(ElevatorCar, best.car_id)
    assert car
    ticket.status = "assigned"
    ticket.assigned_car_id = car.id
    ticket.score = f"{best.score:.1f}"
    car.load += ticket.passengers
    car.floor = ticket.floor
    car.direction = ticket.direction
    db.add(
        DispatchLog(
            call_id=ticket.id,
            car_id=car.id,
            detail=f"派予 {car.label}，评分 {best.score:.1f}（同向/距离综合）",
        )
    )
    db.commit()
    db.refresh(ticket)
    return ticket


@api_router.post("/calls/{call_id}/cancel", response_model=CallOut)
def cancel_call(call_id: int, db: Session = Depends(get_db)):
    ticket = db.get(CallTicket, call_id)
    if not ticket:
        raise HTTPException(404, "呼梯不存在")
    if ticket.status == "rejected":
        raise HTTPException(400, "已拒派呼梯不可取消")
    if ticket.status == "cancelled":
        raise HTTPException(400, "呼梯已取消")
    if ticket.status != "assigned":
        # waiting：仅落状态，待派列表与拥堵均按 waiting 过滤，自动不再计入
        ticket.status = "cancelled"
        db.add(DispatchLog(call_id=ticket.id, car_id=None, detail="乘客取消等待呼梯（未派工，无载荷回退）"))
        db.commit()
        db.refresh(ticket)
        return ticket

    car = db.get(ElevatorCar, ticket.assigned_car_id)
    ticket.status = "cancelled"
    detail = "乘客取消（轿厢记录缺失，无法回退载荷）"
    if car is not None:
        # 回退派工时占用的轿厢载荷；无人在厢时恢复空闲，楼层保持
        before_load = car.load
        car.load = max(0, car.load - ticket.passengers)
        if car.load == 0:
            car.direction = "idle"
        detail = (
            f"乘客取消，{car.label} 载荷回退 {before_load}→{car.load}"
            + ("，轿厢恢复空闲" if car.load == 0 else "")
        )
    db.add(
        DispatchLog(
            call_id=ticket.id,
            car_id=car.id if car else None,
            detail=detail,
        )
    )
    db.commit()
    db.refresh(ticket)
    return ticket


@api_router.get("/replay", response_model=list[LogOut])
def replay(db: Session = Depends(get_db)):
    return db.scalars(select(DispatchLog).order_by(DispatchLog.id.desc())).all()


@api_router.get("/congestion", response_model=list[CongestionFloor])
def congestion(db: Session = Depends(get_db)):
    # 仅未派工的呼梯构成楼层等待；已派工的乘客计入轿厢载荷，已取消/拒派为终态
    waiting = db.scalars(
        select(CallTicket).where(CallTicket.status == "waiting")
    ).all()
    counts = congestion_by_floor(
        [CallRequest(c.id, c.floor, c.direction, c.passengers) for c in waiting]
    )
    return [
        CongestionFloor(floor=f, passengers=p)
        for f, p in sorted(counts.items(), key=lambda x: -x[1])
    ]
