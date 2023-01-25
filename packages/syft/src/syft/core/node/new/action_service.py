# stdlib
import types
from typing import Any
from typing import Callable
from typing import List

# third party
import numpy as np
from result import Err
from result import Ok
from result import Result
from typing_extensions import Self

# relative
from ....core.node.common.node_table.syft_object import transform
from ...common.serde.serializable import serializable
from ...common.uid import UID
from .action_object import Action
from .action_object import ActionObject
from .action_object import ActionObjectPointer
from .action_store import ActionStore
from .context import AuthedServiceContext
from .service import AbstractService
from .service import service_method


@serializable(recursive_serde=True)
class NumpyArrayObjectPointer(ActionObjectPointer):
    __canonical_name__ = "NumpyArrayObjectPointer"
    __version__ = 1

    # 🟡 TODO 17: add state / allowlist inheritance to SyftObject and ignore methods by default
    __attr_state__ = [
        "id",
        "node_uid",
        "parent_id",
    ]

    def __post_init__(self) -> None:
        self.setup_methods()

    def setup_methods(self) -> None:
        infix_operations = ["__add__", "__sub__", "__eq__"]
        for op in infix_operations:
            setattr(
                type(self),
                op,
                types.MethodType(self.__make_infix_op__(op), type(self)),
            )

    def __make_infix_op__(self, op: str) -> Callable:
        def infix_op(_self, other: Any) -> Self:
            if not isinstance(other, ActionObjectPointer):
                # if not isinstance(other, ActionObject):
                #     if not isinstance(other, np.ndarray):
                #         other = np.array(other)
                #     other = NumpyArrayObject(
                #                 syft_action_data=other,
                #                 dtype=other.dtype,
                #                 shape=other.shape
                #             )
                other = other.to_pointer(self.node_uid)
                # print("🔵 TODO: pointerize")
                # raise Exception("We need to pointerize first")
            action = self.make_method_action(op=op, args=[other])
            action_result = self.execute_action(action, sync=True)
            return action_result

        infix_op.__name__ = op
        return infix_op

    def get_from(self, domain_client) -> Any:
        return domain_client.api.services.action.get(self.id).syft_action_data


def numpy_like_eq(left: Any, right: Any) -> bool:
    result = left == right
    if isinstance(result, bool):
        return result

    if hasattr(result, "all"):
        return (result).all()
    return bool(result)


# 🔵 TODO 7: Map TPActionObjects and their 3rd Party types like numpy type to these
# classes for bi-directional lookup.
@serializable(recursive_serde=True)
class NumpyArrayObject(ActionObject, np.lib.mixins.NDArrayOperatorsMixin):
    __canonical_name__ = "NumpyArrayObject"
    __version__ = 1

    syft_pointer_type = NumpyArrayObjectPointer

    def __eq__(self, other: Any) -> bool:
        # 🟡 TODO 8: move __eq__ to a Data / Serdeable type interface on ActionObject
        if isinstance(other, NumpyArrayObject):
            return (
                numpy_like_eq(self.syft_action_data, other.syft_action_data)
                and self.syft_pointer_type == other.syft_pointer_type
            )
        return self == other

    def send_to(self, domain_node) -> NumpyArrayObjectPointer:
        return domain_node.api.services.action.set(self)

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        inputs = tuple(
            np.array(x.syft_action_data, dtype=x.dtype.syft_action_data)
            if isinstance(x, NumpyArrayObject)
            else x
            for x in inputs
        )

        result = getattr(ufunc, method)(*inputs, **kwargs)
        if type(result) is tuple:
            return tuple(
                NumpyArrayObject(syft_action_data=x, dtype=x.dtype, shape=x.shape)
                for x in result
            )
        else:
            return NumpyArrayObject(
                syft_action_data=result, dtype=result.dtype, shape=result.shape
            )


# def expose_dtype(output: dict) -> dict:
#     output["public_dtype"] = output["dtype"]
#     del output["dtype"]
#     return output


# def expose_shape(output: dict) -> dict:
#     output["public_shape"] = output["shape"]
#     del output["shape"]
#     return output


@transform(NumpyArrayObject, NumpyArrayObjectPointer)
def np_array_to_pointer() -> List[Callable]:
    return [
        # expose_dtype,
        # expose_shape,
    ]


class ActionService(AbstractService):
    def __init__(self, store: ActionStore = ActionStore()) -> None:
        self.store = store

    @service_method(path="action.peek", name="peek")
    def peek(self) -> Any:
        return Ok(self.store.data)

    @service_method(path="action.set", name="set")
    def set(
        self, context: AuthedServiceContext, action_object: ActionObject
    ) -> Result[ActionObjectPointer, str]:
        """Save an object to the action store"""
        # 🟡 TODO 9: Create some kind of type checking / protocol for SyftSerializable
        result = self.store.set(
            uid=action_object.id,
            credentials=context.credentials,
            syft_object=action_object,
        )
        if result.is_ok():
            return Ok(action_object.to_pointer(context.node.id))
        return result.err()

    @service_method(path="action.get", name="get")
    def get(self, context: AuthedServiceContext, uid: UID) -> Result[ActionObject, str]:
        """Get an object from the action store"""
        result = self.store.get(uid=uid, credentials=context.credentials)
        if result.is_ok():
            return Ok(result.ok())
        return Err(result.err())

    @service_method(path="action.execute", name="execute")
    def execute(
        self, context: AuthedServiceContext, action: Action
    ) -> Result[ActionObjectPointer, Err]:
        """Execute an operation on objects in the action store"""
        print(action.remote_self)
        print(action.parent_id)
        print(action.args)
        resolved_self = self.get(context=context, uid=action.remote_self)
        if resolved_self.is_err():
            return resolved_self.err()
        else:
            resolved_self = resolved_self.ok().syft_action_data
        args = []
        if action.args:
            for arg_id in action.args:
                arg_value = self.get(context=context, uid=arg_id)
                if arg_value.is_err():
                    return arg_value.err()
                args.append(arg_value.ok().syft_action_data)

        kwargs = {}
        if action.kwargs:
            for key, arg_id in action.kwargs.items():
                kwarg_value = self.get(context=context, uid=arg_id)
                if kwarg_value.is_err():
                    return kwarg_value.err()
                kwargs[key] = kwarg_value.ok().syft_action_data

        # 🔵 TODO 10: Get proper code From old RunClassMethodAction to ensure the function
        # is not bound to the original object or mutated
        target_method = getattr(resolved_self, action.op, None)
        print(target_method)
        print(resolved_self)
        result = None
        try:
            if target_method:
                result = target_method(*args, **kwargs)
        except Exception as e:
            print("what is this exception", e)
            return Err(e)

        print(result)
        # 🟡 TODO 11: Figure out how we want to store action object results
        if isinstance(result, np.ndarray):
            result_action_object = NumpyArrayObject(
                id=action.result_id, parent_id=action.id, syft_action_data=result
            )
        else:
            # 🔵 TODO 12: Create an AnyPointer to handle unexpected results
            result_action_object = ActionObject(
                id=action.result_id, parent_id=action.id, syft_action_data=result  # type: ignore
            )

        set_result = self.store.set(
            uid=action.result_id,
            credentials=context.credentials,
            syft_object=result_action_object,
        )
        if set_result.is_err():
            return set_result.err()

        print(result_action_object)
        return Ok(result_action_object.to_pointer(node_uid=context.node.id))
