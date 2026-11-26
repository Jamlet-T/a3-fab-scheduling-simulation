"""可保存的生产协程协议：由SimPy Process驱动，阶段保存在普通对象中。"""


class Routine:
    """实现generator协议但不持有Python执行帧；子类send返回下一个SimPy事件。"""

    def __iter__(self):
        return self

    def __next__(self):
        return self.send(None)

    def send(self, value):
        raise NotImplementedError

    def throw(self, error, value=None, traceback=None):
        if isinstance(error, BaseException):
            raise error
        raise error() if value is None else error(value)

    def close(self):
        return None
