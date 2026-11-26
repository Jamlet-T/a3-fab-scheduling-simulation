"""生产输入缓冲：容量竞争、重复预留拒绝与token一致性校验。"""

import pytest
import simpy

from fabcosim.production.transfer import ProductionTransferBuffers


def test_capacity_one_makes_the_second_lot_wait_for_the_first_release():
    env = simpy.Environment()
    buffers = ProductionTransferBuffers(env, ["IN_A"], capacity=1)
    admitted = []

    def lot(lot_id):
        yield from buffers.reserve(lot_id, "IN_A")
        admitted.append((lot_id, env.now))
        yield env.timeout(2)
        buffers.release(lot_id)
        assert lot_id not in buffers.tokens

    env.process(lot("L1"))
    env.process(lot("L2"))
    env.run()
    # 容量1：第二lot必须等第一lot释放后才获准，而不是同时进入。
    assert [lot_id for lot_id, _ in admitted] == ["L1", "L2"]
    assert admitted[0][1] == 0 and admitted[1][1] == 2
    assert buffers.tokens == {} and buffers.verify()


def test_reserve_rejects_a_lot_that_already_claims_a_slot():
    env = simpy.Environment()
    buffers = ProductionTransferBuffers(env, ["IN_A"], capacity=1)
    # 第一次预留尚未被推进（还没获准），但lot已登记，不能被静默覆盖。
    buffers.reserve("L1", "IN_A")
    with pytest.raises(ValueError, match="L1"):
        buffers.reserve("L1", "IN_A")


def test_reserve_rejects_a_lot_already_holding_a_token():
    env = simpy.Environment()
    buffers = ProductionTransferBuffers(env, ["IN_A"], capacity=1)
    errors = []

    def lot():
        yield from buffers.reserve("L1", "IN_A")
        try:
            buffers.reserve("L1", "IN_A")
        except ValueError as exc:
            errors.append(str(exc))
        yield env.timeout(1)
        buffers.release("L1")

    env.process(lot())
    env.run()
    assert errors and "L1" in errors[0]
    assert buffers.tokens == {} and buffers.verify()


def test_unknown_target_is_rejected():
    env = simpy.Environment()
    buffers = ProductionTransferBuffers(env, ["IN_A"], capacity=1)
    with pytest.raises(ValueError, match="未知"):
        buffers.reserve("L1", "NOPE")


def test_verify_detects_tokens_that_do_not_match_the_resource_users():
    env = simpy.Environment()
    buffers = ProductionTransferBuffers(env, ["IN_A"], capacity=1)

    def lot():
        yield from buffers.reserve("L1", "IN_A")
        assert buffers.verify()
        # 伪造：token声称占位，但该request并未出现在users里。
        buffers.tokens["L9"] = ("IN_A", object())
        assert buffers.verify() is False
        # 伪造：token指向不存在的设备。
        buffers.tokens["L9"] = ("NOPE", object())
        assert buffers.verify() is False
        del buffers.tokens["L9"]
        assert buffers.verify()
        yield env.timeout(1)
        buffers.release("L1")
        assert buffers.verify()

    env.process(lot())
    env.run()


def test_release_without_a_token_is_a_no_op():
    env = simpy.Environment()
    buffers = ProductionTransferBuffers(env, ["IN_A"], capacity=1)
    buffers.release("L_MISSING")
    assert buffers.tokens == {} and buffers.verify()
