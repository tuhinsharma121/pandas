from __future__ import annotations

from functools import partial
import operator
import re
from typing import (
    TYPE_CHECKING,
    Union,
)

import numpy as np

from pandas._config.config import get_option

from pandas._libs import (
    lib,
    missing as libmissing,
)
from pandas.compat import (
    pa_version_under10p1,
    pa_version_under13p0,
)

from pandas.core.dtypes.common import (
    is_scalar,
    pandas_dtype,
)
from pandas.core.dtypes.missing import isna

from pandas.core.arrays._arrow_string_mixins import ArrowStringArrayMixin
from pandas.core.arrays.arrow import ArrowExtensionArray
from pandas.core.arrays.boolean import BooleanDtype
from pandas.core.arrays.integer import Int64Dtype
from pandas.core.arrays.numeric import NumericDtype
from pandas.core.arrays.string_ import (
    BaseStringArray,
    StringDtype,
)
from pandas.core.strings.object_array import ObjectStringArrayMixin

if not pa_version_under10p1:
    import pyarrow as pa
    import pyarrow.compute as pc

    from pandas.core.arrays.arrow._arrow_utils import fallback_performancewarning


if TYPE_CHECKING:
    from collections.abc import (
        Callable,
        Sequence,
    )

    from pandas._typing import (
        ArrayLike,
        AxisInt,
        Dtype,
        Scalar,
        Self,
        npt,
    )

    from pandas.core.dtypes.dtypes import ExtensionDtype

    from pandas import Series


ArrowStringScalarOrNAT = Union[str, libmissing.NAType]


def _chk_pyarrow_available() -> None:
    if pa_version_under10p1:
        msg = "pyarrow>=10.0.1 is required for PyArrow backed ArrowExtensionArray."
        raise ImportError(msg)


# TODO: Inherit directly from BaseStringArrayMethods. Currently we inherit from
# ObjectStringArrayMixin because we want to have the object-dtype based methods as
# fallback for the ones that pyarrow doesn't yet support


class ArrowStringArray(ObjectStringArrayMixin, ArrowExtensionArray, BaseStringArray):
    """
    Extension array for string data in a ``pyarrow.ChunkedArray``.

    .. warning::

       ArrowStringArray is considered experimental. The implementation and
       parts of the API may change without warning.

    Parameters
    ----------
    values : pyarrow.Array or pyarrow.ChunkedArray
        The array of data.

    Attributes
    ----------
    None

    Methods
    -------
    None

    See Also
    --------
    :func:`array`
        The recommended function for creating a ArrowStringArray.
    Series.str
        The string methods are available on Series backed by
        a ArrowStringArray.

    Notes
    -----
    ArrowStringArray returns a BooleanArray for comparison methods.

    Examples
    --------
    >>> pd.array(["This is", "some text", None, "data."], dtype="string[pyarrow]")
    <ArrowStringArray>
    ['This is', 'some text', <NA>, 'data.']
    Length: 4, dtype: string
    """

    # error: Incompatible types in assignment (expression has type "StringDtype",
    # base class "ArrowExtensionArray" defined the type as "ArrowDtype")
    _dtype: StringDtype  # type: ignore[assignment]
    _storage = "pyarrow"
    _na_value: libmissing.NAType | float = libmissing.NA

    def __init__(self, values) -> None:
        _chk_pyarrow_available()
        if isinstance(values, (pa.Array, pa.ChunkedArray)) and (
            pa.types.is_string(values.type)
            or (
                pa.types.is_dictionary(values.type)
                and (
                    pa.types.is_string(values.type.value_type)
                    or pa.types.is_large_string(values.type.value_type)
                )
            )
        ):
            values = pc.cast(values, pa.large_string())

        super().__init__(values)
        self._dtype = StringDtype(storage=self._storage, na_value=self._na_value)

        if not pa.types.is_large_string(self._pa_array.type):
            raise ValueError(
                "ArrowStringArray requires a PyArrow (chunked) array of "
                "large_string type"
            )

    @classmethod
    def _box_pa_scalar(cls, value, pa_type: pa.DataType | None = None) -> pa.Scalar:
        pa_scalar = super()._box_pa_scalar(value, pa_type)
        if pa.types.is_string(pa_scalar.type) and pa_type is None:
            pa_scalar = pc.cast(pa_scalar, pa.large_string())
        return pa_scalar

    @classmethod
    def _box_pa_array(
        cls, value, pa_type: pa.DataType | None = None, copy: bool = False
    ) -> pa.Array | pa.ChunkedArray:
        pa_array = super()._box_pa_array(value, pa_type)
        if pa.types.is_string(pa_array.type) and pa_type is None:
            pa_array = pc.cast(pa_array, pa.large_string())
        return pa_array

    def __len__(self) -> int:
        """
        Length of this array.

        Returns
        -------
        length : int
        """
        return len(self._pa_array)

    @classmethod
    def _from_sequence(
        cls, scalars, *, dtype: Dtype | None = None, copy: bool = False
    ) -> Self:
        from pandas.core.arrays.masked import BaseMaskedArray

        _chk_pyarrow_available()

        if dtype and not (isinstance(dtype, str) and dtype == "string"):
            dtype = pandas_dtype(dtype)
            assert isinstance(dtype, StringDtype) and dtype.storage == "pyarrow"

        if isinstance(scalars, BaseMaskedArray):
            # avoid costly conversion to object dtype in ensure_string_array and
            # numerical issues with Float32Dtype
            na_values = scalars._mask
            result = scalars._data
            result = lib.ensure_string_array(result, copy=copy, convert_na_value=False)
            return cls(pa.array(result, mask=na_values, type=pa.large_string()))
        elif isinstance(scalars, (pa.Array, pa.ChunkedArray)):
            return cls(pc.cast(scalars, pa.large_string()))

        # convert non-na-likes to str
        result = lib.ensure_string_array(scalars, copy=copy)
        return cls(pa.array(result, type=pa.large_string(), from_pandas=True))

    @classmethod
    def _from_sequence_of_strings(
        cls, strings, *, dtype: ExtensionDtype, copy: bool = False
    ) -> Self:
        return cls._from_sequence(strings, dtype=dtype, copy=copy)

    @property
    def dtype(self) -> StringDtype:  # type: ignore[override]
        """
        An instance of 'string[pyarrow]'.
        """
        return self._dtype

    def insert(self, loc: int, item) -> ArrowStringArray:
        if not isinstance(item, str) and item is not libmissing.NA:
            raise TypeError("Scalar must be NA or str")
        return super().insert(loc, item)

    @classmethod
    def _result_converter(cls, values, na=None):
        return BooleanDtype().__from_arrow__(values)

    def _maybe_convert_setitem_value(self, value):
        """Maybe convert value to be pyarrow compatible."""
        if is_scalar(value):
            if isna(value):
                value = None
            elif not isinstance(value, str):
                raise TypeError("Scalar must be NA or str")
        else:
            value = np.array(value, dtype=object, copy=True)
            value[isna(value)] = None
            for v in value:
                if not (v is None or isinstance(v, str)):
                    raise TypeError("Scalar must be NA or str")
        return super()._maybe_convert_setitem_value(value)

    def isin(self, values: ArrayLike) -> npt.NDArray[np.bool_]:
        value_set = [
            pa_scalar.as_py()
            for pa_scalar in [pa.scalar(value, from_pandas=True) for value in values]
            if pa_scalar.type in (pa.string(), pa.null(), pa.large_string())
        ]

        # short-circuit to return all False array.
        if not len(value_set):
            return np.zeros(len(self), dtype=bool)

        result = pc.is_in(
            self._pa_array, value_set=pa.array(value_set, type=self._pa_array.type)
        )
        # pyarrow 2.0.0 returned nulls, so we explicily specify dtype to convert nulls
        # to False
        return np.array(result, dtype=np.bool_)

    def astype(self, dtype, copy: bool = True):
        dtype = pandas_dtype(dtype)

        if dtype == self.dtype:
            if copy:
                return self.copy()
            return self
        elif isinstance(dtype, NumericDtype):
            data = self._pa_array.cast(pa.from_numpy_dtype(dtype.numpy_dtype))
            return dtype.__from_arrow__(data)
        elif isinstance(dtype, np.dtype) and np.issubdtype(dtype, np.floating):
            return self.to_numpy(dtype=dtype, na_value=np.nan)

        return super().astype(dtype, copy=copy)

    # ------------------------------------------------------------------------
    # String methods interface

    # error: Incompatible types in assignment (expression has type "NAType",
    # base class "ObjectStringArrayMixin" defined the type as "float")
    _str_na_value = libmissing.NA  # type: ignore[assignment]

    _str_map = BaseStringArray._str_map

    def _str_contains(
        self, pat, case: bool = True, flags: int = 0, na=np.nan, regex: bool = True
    ):
        if flags:
            if get_option("mode.performance_warnings"):
                fallback_performancewarning()
            return super()._str_contains(pat, case, flags, na, regex)

        if regex:
            result = pc.match_substring_regex(self._pa_array, pat, ignore_case=not case)
        else:
            result = pc.match_substring(self._pa_array, pat, ignore_case=not case)
        result = self._result_converter(result, na=na)
        if not isna(na):
            result[isna(result)] = bool(na)
        return result

    def _str_startswith(self, pat: str | tuple[str, ...], na: Scalar | None = None):
        if isinstance(pat, str):
            result = pc.starts_with(self._pa_array, pattern=pat)
        else:
            if len(pat) == 0:
                # mimic existing behaviour of string extension array
                # and python string method
                result = pa.array(
                    np.zeros(len(self._pa_array), dtype=bool), mask=isna(self._pa_array)
                )
            else:
                result = pc.starts_with(self._pa_array, pattern=pat[0])

                for p in pat[1:]:
                    result = pc.or_(result, pc.starts_with(self._pa_array, pattern=p))
        if not isna(na):
            result = result.fill_null(na)
        return self._result_converter(result)

    def _str_endswith(self, pat: str | tuple[str, ...], na: Scalar | None = None):
        if isinstance(pat, str):
            result = pc.ends_with(self._pa_array, pattern=pat)
        else:
            if len(pat) == 0:
                # mimic existing behaviour of string extension array
                # and python string method
                result = pa.array(
                    np.zeros(len(self._pa_array), dtype=bool), mask=isna(self._pa_array)
                )
            else:
                result = pc.ends_with(self._pa_array, pattern=pat[0])

                for p in pat[1:]:
                    result = pc.or_(result, pc.ends_with(self._pa_array, pattern=p))
        if not isna(na):
            result = result.fill_null(na)
        return self._result_converter(result)

    def _str_replace(
        self,
        pat: str | re.Pattern,
        repl: str | Callable,
        n: int = -1,
        case: bool = True,
        flags: int = 0,
        regex: bool = True,
    ):
        if isinstance(pat, re.Pattern) or callable(repl) or not case or flags:
            if get_option("mode.performance_warnings"):
                fallback_performancewarning()
            return super()._str_replace(pat, repl, n, case, flags, regex)

        func = pc.replace_substring_regex if regex else pc.replace_substring
        result = func(self._pa_array, pattern=pat, replacement=repl, max_replacements=n)
        return type(self)(result)

    def _str_repeat(self, repeats: int | Sequence[int]):
        if not isinstance(repeats, int):
            return super()._str_repeat(repeats)
        else:
            return type(self)(pc.binary_repeat(self._pa_array, repeats))

    def _str_match(
        self, pat: str, case: bool = True, flags: int = 0, na: Scalar | None = None
    ):
        if not pat.startswith("^"):
            pat = f"^{pat}"
        return self._str_contains(pat, case, flags, na, regex=True)

    def _str_fullmatch(
        self, pat, case: bool = True, flags: int = 0, na: Scalar | None = None
    ):
        if not pat.endswith("$") or pat.endswith("\\$"):
            pat = f"{pat}$"
        return self._str_match(pat, case, flags, na)

    def _str_slice(
        self, start: int | None = None, stop: int | None = None, step: int | None = None
    ) -> Self:
        if stop is None:
            return super()._str_slice(start, stop, step)
        if start is None:
            start = 0
        if step is None:
            step = 1
        return type(self)(
            pc.utf8_slice_codeunits(self._pa_array, start=start, stop=stop, step=step)
        )

    def _str_isalnum(self):
        result = pc.utf8_is_alnum(self._pa_array)
        return self._result_converter(result)

    def _str_isalpha(self):
        result = pc.utf8_is_alpha(self._pa_array)
        return self._result_converter(result)

    def _str_isdecimal(self):
        result = pc.utf8_is_decimal(self._pa_array)
        return self._result_converter(result)

    def _str_isdigit(self):
        result = pc.utf8_is_digit(self._pa_array)
        return self._result_converter(result)

    def _str_islower(self):
        result = pc.utf8_is_lower(self._pa_array)
        return self._result_converter(result)

    def _str_isnumeric(self):
        result = pc.utf8_is_numeric(self._pa_array)
        return self._result_converter(result)

    def _str_isspace(self):
        result = pc.utf8_is_space(self._pa_array)
        return self._result_converter(result)

    def _str_istitle(self):
        result = pc.utf8_is_title(self._pa_array)
        return self._result_converter(result)

    def _str_isupper(self):
        result = pc.utf8_is_upper(self._pa_array)
        return self._result_converter(result)

    def _str_len(self):
        result = pc.utf8_length(self._pa_array)
        return self._convert_int_dtype(result)

    def _str_lower(self) -> Self:
        return type(self)(pc.utf8_lower(self._pa_array))

    def _str_upper(self) -> Self:
        return type(self)(pc.utf8_upper(self._pa_array))

    def _str_strip(self, to_strip=None) -> Self:
        if to_strip is None:
            result = pc.utf8_trim_whitespace(self._pa_array)
        else:
            result = pc.utf8_trim(self._pa_array, characters=to_strip)
        return type(self)(result)

    def _str_lstrip(self, to_strip=None) -> Self:
        if to_strip is None:
            result = pc.utf8_ltrim_whitespace(self._pa_array)
        else:
            result = pc.utf8_ltrim(self._pa_array, characters=to_strip)
        return type(self)(result)

    def _str_rstrip(self, to_strip=None) -> Self:
        if to_strip is None:
            result = pc.utf8_rtrim_whitespace(self._pa_array)
        else:
            result = pc.utf8_rtrim(self._pa_array, characters=to_strip)
        return type(self)(result)

    def _str_removeprefix(self, prefix: str):
        if not pa_version_under13p0:
            starts_with = pc.starts_with(self._pa_array, pattern=prefix)
            removed = pc.utf8_slice_codeunits(self._pa_array, len(prefix))
            result = pc.if_else(starts_with, removed, self._pa_array)
            return type(self)(result)
        return super()._str_removeprefix(prefix)

    def _str_removesuffix(self, suffix: str):
        ends_with = pc.ends_with(self._pa_array, pattern=suffix)
        removed = pc.utf8_slice_codeunits(self._pa_array, 0, stop=-len(suffix))
        result = pc.if_else(ends_with, removed, self._pa_array)
        return type(self)(result)

    def _str_count(self, pat: str, flags: int = 0):
        if flags:
            return super()._str_count(pat, flags)
        result = pc.count_substring_regex(self._pa_array, pat)
        return self._convert_int_dtype(result)

    def _str_find(self, sub: str, start: int = 0, end: int | None = None):
        if start != 0 and end is not None:
            slices = pc.utf8_slice_codeunits(self._pa_array, start, stop=end)
            result = pc.find_substring(slices, sub)
            not_found = pc.equal(result, -1)
            offset_result = pc.add(result, end - start)
            result = pc.if_else(not_found, result, offset_result)
        elif start == 0 and end is None:
            slices = self._pa_array
            result = pc.find_substring(slices, sub)
        else:
            return super()._str_find(sub, start, end)
        return self._convert_int_dtype(result)

    def _str_get_dummies(self, sep: str = "|"):
        dummies_pa, labels = ArrowExtensionArray(self._pa_array)._str_get_dummies(sep)
        if len(labels) == 0:
            return np.empty(shape=(0, 0), dtype=np.int64), labels
        dummies = np.vstack(dummies_pa.to_numpy())
        return dummies.astype(np.int64, copy=False), labels

    def _convert_int_dtype(self, result):
        return Int64Dtype().__from_arrow__(result)

    def _reduce(
        self, name: str, *, skipna: bool = True, keepdims: bool = False, **kwargs
    ):
        result = self._reduce_calc(name, skipna=skipna, keepdims=keepdims, **kwargs)
        if name in ("argmin", "argmax") and isinstance(result, pa.Array):
            return self._convert_int_dtype(result)
        elif isinstance(result, pa.Array):
            return type(self)(result)
        else:
            return result

    def _rank(
        self,
        *,
        axis: AxisInt = 0,
        method: str = "average",
        na_option: str = "keep",
        ascending: bool = True,
        pct: bool = False,
    ):
        """
        See Series.rank.__doc__.
        """
        return self._convert_int_dtype(
            self._rank_calc(
                axis=axis,
                method=method,
                na_option=na_option,
                ascending=ascending,
                pct=pct,
            )
        )


class ArrowStringArrayNumpySemantics(ArrowStringArray):
    _storage = "pyarrow"
    _na_value = np.nan

    @classmethod
    def _result_converter(cls, values, na=None):
        if not isna(na):
            values = values.fill_null(bool(na))
        return ArrowExtensionArray(values).to_numpy(na_value=np.nan)

    def __getattribute__(self, item):
        # ArrowStringArray and we both inherit from ArrowExtensionArray, which
        # creates inheritance problems (Diamond inheritance)
        if item in ArrowStringArrayMixin.__dict__ and item not in (
            "_pa_array",
            "__dict__",
        ):
            return partial(getattr(ArrowStringArrayMixin, item), self)
        return super().__getattribute__(item)

    def _convert_int_dtype(self, result):
        if isinstance(result, pa.Array):
            result = result.to_numpy(zero_copy_only=False)
        else:
            result = result.to_numpy()
        if result.dtype == np.int32:
            result = result.astype(np.int64)
        return result

    def _cmp_method(self, other, op):
        result = super()._cmp_method(other, op)
        if op == operator.ne:
            return result.to_numpy(np.bool_, na_value=True)
        else:
            return result.to_numpy(np.bool_, na_value=False)

    def value_counts(self, dropna: bool = True) -> Series:
        from pandas import Series

        result = super().value_counts(dropna)
        return Series(
            result._values.to_numpy(), index=result.index, name=result.name, copy=False
        )

    def _reduce(
        self, name: str, *, skipna: bool = True, keepdims: bool = False, **kwargs
    ):
        if name in ["any", "all"]:
            if not skipna:
                nas = pc.is_null(self._pa_array)
                arr = pc.or_kleene(nas, pc.not_equal(self._pa_array, ""))
            else:
                arr = pc.not_equal(self._pa_array, "")
            return ArrowExtensionArray(arr)._reduce(
                name, skipna=skipna, keepdims=keepdims, **kwargs
            )
        else:
            return super()._reduce(name, skipna=skipna, keepdims=keepdims, **kwargs)

    def insert(self, loc: int, item) -> ArrowStringArrayNumpySemantics:
        if item is np.nan:
            item = libmissing.NA
        return super().insert(loc, item)  # type: ignore[return-value]
