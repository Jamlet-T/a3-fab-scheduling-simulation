"""纯生产流程的显式执行位置；由原生SimPy Process保存事件/资源等待关系。"""

from fabcosim.production.model import grid_delay
from fabcosim.production.routine import Routine


class ReserveRoutine(Routine):
    def __init__(self, buffers, lot_id, target):
        self.buffers, self.lot_id, self.target = buffers, lot_id, target
        self.waiting = False

    def send(self, value):
        if not self.waiting:
            self.request = self.buffers.resources[self.target].request()
            self.waiting = True
            return self.request
        self.buffers._pending.discard(self.lot_id)
        self.buffers.tokens[self.lot_id] = (self.target, self.request)
        raise StopIteration


class TransferRoutine(Routine):
    def __init__(self, context, lot_id, target, op):
        self.c, self.lot_id, self.target, self.op = context, lot_id, target, op
        self.pc = "request"

    def send(self, value):
        c, lid, target, op = self.c, self.lot_id, self.target, self.op
        state, lot = c.states[lid], c.lots[lid]
        if self.pc == "request":
            c.locate(lot, state["equipment"] + ".tool")
            state["phase"] = "waiting_production_buffer"
            c.log_lot("production.transfer_requested", lid, op, equipment=target)
            self.pc = "reserved"
            return c.env.process(c.buffers.reserve(lid, target))
        if self.pc == "reserved":
            state["phase"] = "production_transfer"
            c.log_lot("production.transfer_start", lid, op, equipment=target, duration_s=c.transfer_s)
            self.pc = "arrived"
            return c.env.timeout(grid_delay(c.env.now, c.transfer_s, c.step))
        state["equipment"] = target
        c.locate(lot, target + ".queue")
        c.log_lot("production.transfer_end", lid, op, equipment=target)
        raise StopIteration


class BatchRoutine(Routine):
    def __init__(self, context, name, batch):
        self.c, self.name, self.batch = context, name, batch
        self.started = False

    def sample(self):
        return self.c.sample(self.batch[0])

    def log(self, kind, **fields):
        self.c.operation_log(kind, self.batch, **fields)

    def send(self, value):
        if not self.started:
            self.c.batch_formed(self.name, self.batch)
            self.started = True
            return self.c.env.process(self.c.machines[self.name].process(
                self.batch[0]["op"],
                [self.c.states[j["lot_id"]]["wafer_count"] for j in self.batch],
                self.sample, self.log,
            ))
        self.c.finish_batch(self.name, self.batch)
        raise StopIteration


class DeadlineRoutine(Routine):
    def __init__(self, context, name):
        self.c, self.name, self.waiting = context, name, False

    def send(self, value):
        if not self.waiting:
            self.waiting = True
            return self.c.env.timeout(grid_delay(
                self.c.env.now, self.c.config["production"]["batch_wait_s"], self.c.step
            ))
        self.c.dispatch_machine(self.name)
        raise StopIteration


class LotRoutine(Routine):
    def __init__(self, context, definition):
        self.c, self.definition = context, definition
        self.lot_id, self.pc = definition["id"], "release"
        self.delay_request = None

    def send(self, value):
        c, lid = self.c, self.lot_id
        state, lot = c.states[lid], c.lots[lid]
        while True:
            if self.pc == "release":
                self.pc = "released"
                return c.env.timeout(grid_delay(0, self.definition["release_s"], c.step))
            if self.pc == "released":
                c.release_lot(self.definition)
                self.pc = "choose"
            if self.pc == "choose":
                self.op = c.next_operation(lid)
                if self.op is None:
                    self.pc = "exit"
                    target = sorted(c.config["candidates"]["COMPLETE"])[0]
                    return c.env.process(c.transport(lid, target, None))
                self.target = c.destination(lid, self.op)
                state["next_port"] = c.assets[self.target]["input_port"]
                c.log_lot("destination.selected", lid, self.op, equipment=self.target)
                self.pc = "arrived"
                return c.env.process(c.transport(lid, self.target, self.op))
            if self.pc == "arrived":
                state["phase"] = "waiting_tool"
                if self.op["virtual"]:
                    self.delay_request = c.delay_resources.request()
                    self.pc = "delay_start"
                    return self.delay_request
                self.pc = "finished_op"
                return c.enqueue_job(lid, self.target, self.op)["done"]
            if self.pc == "delay_start":
                state["phase"] = "process_delay"
                c.log_lot("delay.start", lid, self.op, equipment=self.target)
                self.pc = "delay_end"
                return c.env.timeout(grid_delay(
                    c.env.now, c.sample({"op": self.op, "lot_id": lid}), c.step
                ))
            if self.pc == "delay_end":
                lot.completed_operations += 1
                state["processed_steps"].append(self.op["source_step"])
                c.log_lot("delay.end", lid, self.op, equipment=self.target)
                c.delay_resources.release(self.delay_request)
                self.delay_request = None
                self.pc = "finished_op"
            if self.pc == "finished_op":
                c.after_operation(lid, self.op)
                self.pc = "choose"
                continue
            if self.pc == "exit":
                c.finish_lot(lid)
                raise StopIteration

    def throw(self, error, value=None, traceback=None):
        if self.delay_request is not None:
            self.c.delay_resources.release(self.delay_request)
            self.delay_request = None
        return super().throw(error, value, traceback)
