"""
Contains all classes and functions to deal with lists, dicts, generators and
iterators in general.

Array modifications
*******************

If the content of an array (``set``/``list``) is requested somewhere, the
current module will be checked for appearances of ``arr.append``,
``arr.insert``, etc.  If the ``arr`` name points to an actual array, the
content will be added

This can be really cpu intensive, as you can imagine. Because |jedi| has to
follow **every** ``append`` and check wheter it's the right array. However this
works pretty good, because in *slow* cases, the recursion detector and other
settings will stop this process.

It is important to note that:

1. Array modfications work only in the current module.
2. Jedi only checks Array additions; ``list.pop``, etc are ignored.
"""
import sys

from jedi import debug
from jedi import settings
from jedi._compatibility import force_unicode, is_py3
from jedi.inference import compiled
from jedi.inference import analysis
from jedi.inference import recursion
from jedi.inference.lazy_value import LazyKnownValue, LazyKnownValues, \
    LazyTreeValue
from jedi.inference.helpers import get_int_or_none, is_string, \
    predefine_names, infer_call_of_leaf, reraise_getitem_errors, \
    SimpleGetItemNotFound
from jedi.inference.utils import safe_property, to_list
from jedi.inference.cache import inference_state_method_cache
from jedi.inference.filters import ParserTreeFilter, LazyAttributeOverwrite, \
    publish_method
from jedi.inference.base_value import ValueSet, Value, NO_VALUES, \
    TreeValue, ContextualizedNode, iterate_values, HelperValueMixin, _sentinel
from jedi.parser_utils import get_sync_comp_fors


class IterableMixin(object):
    def py__stop_iteration_returns(self):
        return ValueSet([compiled.builtin_from_name(self.inference_state, u'None')])

    # At the moment, safe values are simple values like "foo", 1 and not
    # lists/dicts. Therefore as a small speed optimization we can just do the
    # default instead of resolving the lazy wrapped values, that are just
    # doing this in the end as well.
    # This mostly speeds up patterns like `sys.version_info >= (3, 0)` in
    # typeshed.
    if sys.version_info[0] == 2:
        # Python 2...........
        def get_safe_value(self, default=_sentinel):
            if default is _sentinel:
                raise ValueError("There exists no safe value for value %s" % self)
            return default
    else:
        get_safe_value = Value.get_safe_value


class GeneratorBase(LazyAttributeOverwrite, IterableMixin):
    array_type = None

    def _get_wrapped_value(self):
        generator, = self.inference_state.typing_module \
            .py__getattribute__('Generator') \
            .execute_annotation()
        return generator

    def is_instance(self):
        return False

    def py__bool__(self):
        return True

    @publish_method('__iter__')
    def py__iter__(self, valueualized_node=None):
        return ValueSet([self])

    @publish_method('send')
    @publish_method('next', python_version_match=2)
    @publish_method('__next__', python_version_match=3)
    def py__next__(self):
        return ValueSet.from_sets(lazy_value.infer() for lazy_value in self.py__iter__())

    def py__stop_iteration_returns(self):
        return ValueSet([compiled.builtin_from_name(self.inference_state, u'None')])

    @property
    def name(self):
        return compiled.CompiledValueName(self, 'Generator')


class Generator(GeneratorBase):
    """Handling of `yield` functions."""
    def __init__(self, inference_state, func_execution_context):
        super(Generator, self).__init__(inference_state)
        self._func_execution_context = func_execution_context

    def py__iter__(self, valueualized_node=None):
        return self._func_execution_context.get_yield_lazy_values()

    def py__stop_iteration_returns(self):
        return self._func_execution_context.get_return_values()

    def __repr__(self):
        return "<%s of %s>" % (type(self).__name__, self._func_execution_context)


class CompForValue(TreeValue):
    @classmethod
    def from_comp_for(cls, parent_context, comp_for):
        return cls(parent_context.inference_state, parent_context, comp_for)

    def get_filters(self, until_position=None, origin_scope=None):
        yield ParserTreeFilter(self)


def comprehension_from_atom(inference_state, value, atom):
    bracket = atom.children[0]
    test_list_comp = atom.children[1]

    if bracket == '{':
        if atom.children[1].children[1] == ':':
            sync_comp_for = test_list_comp.children[3]
            if sync_comp_for.type == 'comp_for':
                sync_comp_for = sync_comp_for.children[1]

            return DictComprehension(
                inference_state,
                value,
                sync_comp_for_node=sync_comp_for,
                key_node=test_list_comp.children[0],
                value_node=test_list_comp.children[2],
            )
        else:
            cls = SetComprehension
    elif bracket == '(':
        cls = GeneratorComprehension
    elif bracket == '[':
        cls = ListComprehension

    sync_comp_for = test_list_comp.children[1]
    if sync_comp_for.type == 'comp_for':
        sync_comp_for = sync_comp_for.children[1]

    return cls(
        inference_state,
        defining_context=value,
        sync_comp_for_node=sync_comp_for,
        entry_node=test_list_comp.children[0],
    )


class ComprehensionMixin(object):
    @inference_state_method_cache()
    def _get_comp_for_value(self, parent_context, comp_for):
        return CompForValue.from_comp_for(parent_context, comp_for)

    def _nested(self, comp_fors, parent_context=None):
        comp_for = comp_fors[0]

        is_async = comp_for.parent.type == 'comp_for'

        input_node = comp_for.children[3]
        parent_context = parent_context or self._defining_context
        input_types = parent_context.infer_node(input_node)
        # TODO: simulate await if self.is_async

        cn = ContextualizedNode(parent_context, input_node)
        iterated = input_types.iterate(cn, is_async=is_async)
        exprlist = comp_for.children[1]
        for i, lazy_value in enumerate(iterated):
            types = lazy_value.infer()
            dct = unpack_tuple_to_dict(parent_context, types, exprlist)
            value_ = self._get_comp_for_value(
                parent_context,
                comp_for,
            )
            with predefine_names(value_, comp_for, dct):
                try:
                    for result in self._nested(comp_fors[1:], value_):
                        yield result
                except IndexError:
                    iterated = value_.infer_node(self._entry_node)
                    if self.array_type == 'dict':
                        yield iterated, value_.infer_node(self._value_node)
                    else:
                        yield iterated

    @inference_state_method_cache(default=[])
    @to_list
    def _iterate(self):
        comp_fors = tuple(get_sync_comp_fors(self._sync_comp_for_node))
        for result in self._nested(comp_fors):
            yield result

    def py__iter__(self, valueualized_node=None):
        for set_ in self._iterate():
            yield LazyKnownValues(set_)

    def __repr__(self):
        return "<%s of %s>" % (type(self).__name__, self._sync_comp_for_node)


class _DictMixin(object):
    def _get_generics(self):
        return tuple(c_set.py__class__() for c_set in self.get_mapping_item_values())


class Sequence(LazyAttributeOverwrite, IterableMixin):
    api_type = u'instance'

    @property
    def name(self):
        return compiled.CompiledValueName(self, self.array_type)

    def _get_generics(self):
        return (self.merge_types_of_iterate().py__class__(),)

    def _get_wrapped_value(self):
        from jedi.inference.gradual.typing import GenericClass
        klass = compiled.builtin_from_name(self.inference_state, self.array_type)
        c, = GenericClass(klass, self._get_generics()).execute_annotation()
        return c

    def py__bool__(self):
        return None  # We don't know the length, because of appends.

    def py__class__(self):
        return compiled.builtin_from_name(self.inference_state, self.array_type)

    @safe_property
    def parent(self):
        return self.inference_state.builtins_module

    def py__getitem__(self, index_value_set, valueualized_node):
        if self.array_type == 'dict':
            return self._dict_values()
        return iterate_values(ValueSet([self]))


class _BaseComprehension(ComprehensionMixin):
    def __init__(self, inference_state, defining_context, sync_comp_for_node, entry_node):
        assert sync_comp_for_node.type == 'sync_comp_for'
        super(_BaseComprehension, self).__init__(inference_state)
        self._defining_context = defining_context
        self._sync_comp_for_node = sync_comp_for_node
        self._entry_node = entry_node


class ListComprehension(_BaseComprehension, Sequence):
    array_type = u'list'

    def py__simple_getitem__(self, index):
        if isinstance(index, slice):
            return ValueSet([self])

        all_types = list(self.py__iter__())
        with reraise_getitem_errors(IndexError, TypeError):
            lazy_value = all_types[index]
        return lazy_value.infer()


class SetComprehension(_BaseComprehension, Sequence):
    array_type = u'set'


class GeneratorComprehension(_BaseComprehension, GeneratorBase):
    pass


class DictComprehension(ComprehensionMixin, Sequence):
    array_type = u'dict'

    def __init__(self, inference_state, defining_context, sync_comp_for_node, key_node, value_node):
        assert sync_comp_for_node.type == 'sync_comp_for'
        super(DictComprehension, self).__init__(inference_state)
        self._defining_context = defining_context
        self._sync_comp_for_node = sync_comp_for_node
        self._entry_node = key_node
        self._value_node = value_node

    def py__iter__(self, valueualized_node=None):
        for keys, values in self._iterate():
            yield LazyKnownValues(keys)

    def py__simple_getitem__(self, index):
        for keys, values in self._iterate():
            for k in keys:
                if isinstance(k, compiled.CompiledObject):
                    # Be careful in the future if refactoring, index could be a
                    # slice.
                    if k.get_safe_value(default=object()) == index:
                        return values
        raise SimpleGetItemNotFound()

    def _dict_keys(self):
        return ValueSet.from_sets(keys for keys, values in self._iterate())

    def _dict_values(self):
        return ValueSet.from_sets(values for keys, values in self._iterate())

    @publish_method('values')
    def _imitate_values(self):
        lazy_value = LazyKnownValues(self._dict_values())
        return ValueSet([FakeSequence(self.inference_state, u'list', [lazy_value])])

    @publish_method('items')
    def _imitate_items(self):
        lazy_values = [
            LazyKnownValue(
                FakeSequence(
                    self.inference_state,
                    u'tuple',
                    [LazyKnownValues(key),
                     LazyKnownValues(value)]
                )
            )
            for key, value in self._iterate()
        ]

        return ValueSet([FakeSequence(self.inference_state, u'list', lazy_values)])

    def get_mapping_item_values(self):
        return self._dict_keys(), self._dict_values()

    def exact_key_items(self):
        # NOTE: A smarter thing can probably done here to achieve better
        # completions, but at least like this jedi doesn't crash
        return []


class SequenceLiteralValue(Sequence):
    _TUPLE_LIKE = 'testlist_star_expr', 'testlist', 'subscriptlist'
    mapping = {'(': u'tuple',
               '[': u'list',
               '{': u'set'}

    def __init__(self, inference_state, defining_context, atom):
        super(SequenceLiteralValue, self).__init__(inference_state)
        self.atom = atom
        self._defining_context = defining_context

        if self.atom.type in self._TUPLE_LIKE:
            self.array_type = u'tuple'
        else:
            self.array_type = SequenceLiteralValue.mapping[atom.children[0]]
            """The builtin name of the array (list, set, tuple or dict)."""

    def py__simple_getitem__(self, index):
        """Here the index is an int/str. Raises IndexError/KeyError."""
        if self.array_type == u'dict':
            compiled_obj_index = compiled.create_simple_object(self.inference_state, index)
            for key, value in self.get_tree_entries():
                for k in self._defining_context.infer_node(key):
                    try:
                        method = k.execute_operation
                    except AttributeError:
                        pass
                    else:
                        if method(compiled_obj_index, u'==').get_safe_value():
                            return self._defining_context.infer_node(value)
            raise SimpleGetItemNotFound('No key found in dictionary %s.' % self)

        if isinstance(index, slice):
            return ValueSet([self])
        else:
            with reraise_getitem_errors(TypeError, KeyError, IndexError):
                node = self.get_tree_entries()[index]
            return self._defining_context.infer_node(node)

    def py__iter__(self, valueualized_node=None):
        """
        While values returns the possible values for any array field, this
        function returns the value for a certain index.
        """
        if self.array_type == u'dict':
            # Get keys.
            types = NO_VALUES
            for k, _ in self.get_tree_entries():
                types |= self._defining_context.infer_node(k)
            # We don't know which dict index comes first, therefore always
            # yield all the types.
            for _ in types:
                yield LazyKnownValues(types)
        else:
            for node in self.get_tree_entries():
                if node == ':' or node.type == 'subscript':
                    # TODO this should probably use at least part of the code
                    #      of infer_subscript_list.
                    yield LazyKnownValue(Slice(self._defining_context, None, None, None))
                else:
                    yield LazyTreeValue(self._defining_context, node)
            for addition in check_array_additions(self._defining_context, self):
                yield addition

    def py__len__(self):
        # This function is not really used often. It's more of a try.
        return len(self.get_tree_entries())

    def _dict_values(self):
        return ValueSet.from_sets(
            self._defining_context.infer_node(v)
            for k, v in self.get_tree_entries()
        )

    def get_tree_entries(self):
        c = self.atom.children

        if self.atom.type in self._TUPLE_LIKE:
            return c[::2]

        array_node = c[1]
        if array_node in (']', '}', ')'):
            return []  # Direct closing bracket, doesn't contain items.

        if array_node.type == 'testlist_comp':
            # filter out (for now) pep 448 single-star unpacking
            return [value for value in array_node.children[::2]
                    if value.type != "star_expr"]
        elif array_node.type == 'dictorsetmaker':
            kv = []
            iterator = iter(array_node.children)
            for key in iterator:
                if key == "**":
                    # dict with pep 448 double-star unpacking
                    # for now ignoring the values imported by **
                    next(iterator)
                    next(iterator, None)  # Possible comma.
                else:
                    op = next(iterator, None)
                    if op is None or op == ',':
                        if key.type == "star_expr":
                            # pep 448 single-star unpacking
                            # for now ignoring values imported by *
                            pass
                        else:
                            kv.append(key)  # A set.
                    else:
                        assert op == ':'  # A dict.
                        kv.append((key, next(iterator)))
                        next(iterator, None)  # Possible comma.
            return kv
        else:
            if array_node.type == "star_expr":
                # pep 448 single-star unpacking
                # for now ignoring values imported by *
                return []
            else:
                return [array_node]

    def exact_key_items(self):
        """
        Returns a generator of tuples like dict.items(), where the key is
        resolved (as a string) and the values are still lazy values.
        """
        for key_node, value in self.get_tree_entries():
            for key in self._defining_context.infer_node(key_node):
                if is_string(key):
                    yield key.get_safe_value(), LazyTreeValue(self._defining_context, value)

    def __repr__(self):
        return "<%s of %s>" % (self.__class__.__name__, self.atom)


class DictLiteralValue(_DictMixin, SequenceLiteralValue):
    array_type = u'dict'

    def __init__(self, inference_state, defining_context, atom):
        super(SequenceLiteralValue, self).__init__(inference_state)
        self._defining_context = defining_context
        self.atom = atom

    @publish_method('values')
    def _imitate_values(self):
        lazy_value = LazyKnownValues(self._dict_values())
        return ValueSet([FakeSequence(self.inference_state, u'list', [lazy_value])])

    @publish_method('items')
    def _imitate_items(self):
        lazy_values = [
            LazyKnownValue(FakeSequence(
                self.inference_state, u'tuple',
                (LazyTreeValue(self._defining_context, key_node),
                 LazyTreeValue(self._defining_context, value_node))
            )) for key_node, value_node in self.get_tree_entries()
        ]

        return ValueSet([FakeSequence(self.inference_state, u'list', lazy_values)])

    def _dict_keys(self):
        return ValueSet.from_sets(
            self._defining_context.infer_node(k)
            for k, v in self.get_tree_entries()
        )

    def get_mapping_item_values(self):
        return self._dict_keys(), self._dict_values()


class _FakeArray(SequenceLiteralValue):
    def __init__(self, inference_state, container, type):
        super(SequenceLiteralValue, self).__init__(inference_state)
        self.array_type = type
        self.atom = container
        # TODO is this class really needed?


class FakeSequence(_FakeArray):
    def __init__(self, inference_state, array_type, lazy_value_list):
        """
        type should be one of "tuple", "list"
        """
        super(FakeSequence, self).__init__(inference_state, None, array_type)
        self._lazy_value_list = lazy_value_list

    def py__simple_getitem__(self, index):
        if isinstance(index, slice):
            return ValueSet([self])

        with reraise_getitem_errors(IndexError, TypeError):
            lazy_value = self._lazy_value_list[index]
        return lazy_value.infer()

    def py__iter__(self, valueualized_node=None):
        return self._lazy_value_list

    def py__bool__(self):
        return bool(len(self._lazy_value_list))

    def __repr__(self):
        return "<%s of %s>" % (type(self).__name__, self._lazy_value_list)


class FakeDict(_DictMixin, _FakeArray):
    def __init__(self, inference_state, dct):
        super(FakeDict, self).__init__(inference_state, dct, u'dict')
        self._dct = dct

    def py__iter__(self, valueualized_node=None):
        for key in self._dct:
            yield LazyKnownValue(compiled.create_simple_object(self.inference_state, key))

    def py__simple_getitem__(self, index):
        if is_py3 and self.inference_state.environment.version_info.major == 2:
            # In Python 2 bytes and unicode compare.
            if isinstance(index, bytes):
                index_unicode = force_unicode(index)
                try:
                    return self._dct[index_unicode].infer()
                except KeyError:
                    pass
            elif isinstance(index, str):
                index_bytes = index.encode('utf-8')
                try:
                    return self._dct[index_bytes].infer()
                except KeyError:
                    pass

        with reraise_getitem_errors(KeyError, TypeError):
            lazy_value = self._dct[index]
        return lazy_value.infer()

    @publish_method('values')
    def _values(self):
        return ValueSet([FakeSequence(
            self.inference_state, u'tuple',
            [LazyKnownValues(self._dict_values())]
        )])

    def _dict_values(self):
        return ValueSet.from_sets(lazy_value.infer() for lazy_value in self._dct.values())

    def _dict_keys(self):
        return ValueSet.from_sets(lazy_value.infer() for lazy_value in self.py__iter__())

    def get_mapping_item_values(self):
        return self._dict_keys(), self._dict_values()

    def exact_key_items(self):
        return self._dct.items()


class MergedArray(_FakeArray):
    def __init__(self, inference_state, arrays):
        super(MergedArray, self).__init__(inference_state, arrays, arrays[-1].array_type)
        self._arrays = arrays

    def py__iter__(self, valueualized_node=None):
        for array in self._arrays:
            for lazy_value in array.py__iter__():
                yield lazy_value

    def py__simple_getitem__(self, index):
        return ValueSet.from_sets(lazy_value.infer() for lazy_value in self.py__iter__())

    def get_tree_entries(self):
        for array in self._arrays:
            for a in array.get_tree_entries():
                yield a

    def __len__(self):
        return sum(len(a) for a in self._arrays)


def unpack_tuple_to_dict(value, types, exprlist):
    """
    Unpacking tuple assignments in for statements and expr_stmts.
    """
    if exprlist.type == 'name':
        return {exprlist.value: types}
    elif exprlist.type == 'atom' and exprlist.children[0] in ('(', '['):
        return unpack_tuple_to_dict(value, types, exprlist.children[1])
    elif exprlist.type in ('testlist', 'testlist_comp', 'exprlist',
                           'testlist_star_expr'):
        dct = {}
        parts = iter(exprlist.children[::2])
        n = 0
        for lazy_value in types.iterate(exprlist):
            n += 1
            try:
                part = next(parts)
            except StopIteration:
                # TODO this value is probably not right.
                analysis.add(value, 'value-error-too-many-values', part,
                             message="ValueError: too many values to unpack (expected %s)" % n)
            else:
                dct.update(unpack_tuple_to_dict(value, lazy_value.infer(), part))
        has_parts = next(parts, None)
        if types and has_parts is not None:
            # TODO this value is probably not right.
            analysis.add(value, 'value-error-too-few-values', has_parts,
                         message="ValueError: need more than %s values to unpack" % n)
        return dct
    elif exprlist.type == 'power' or exprlist.type == 'atom_expr':
        # Something like ``arr[x], var = ...``.
        # This is something that is not yet supported, would also be difficult
        # to write into a dict.
        return {}
    elif exprlist.type == 'star_expr':  # `a, *b, c = x` type unpackings
        # Currently we're not supporting them.
        return {}
    raise NotImplementedError


def check_array_additions(context, sequence):
    """ Just a mapper function for the internal _check_array_additions """
    if sequence.array_type not in ('list', 'set'):
        # TODO also check for dict updates
        return NO_VALUES

    return _check_array_additions(context, sequence)


@inference_state_method_cache(default=NO_VALUES)
@debug.increase_indent
def _check_array_additions(context, sequence):
    """
    Checks if a `Array` has "add" (append, insert, extend) statements:

    >>> a = [""]
    >>> a.append(1)
    """
    from jedi.inference import arguments

    debug.dbg('Dynamic array search for %s' % sequence, color='MAGENTA')
    module_context = context.get_root_context()
    if not settings.dynamic_array_additions or isinstance(module_context, compiled.CompiledObject):
        debug.dbg('Dynamic array search aborted.', color='MAGENTA')
        return NO_VALUES

    def find_additions(value, arglist, add_name):
        params = list(arguments.TreeArguments(value.inference_state, value, arglist).unpack())
        result = set()
        if add_name in ['insert']:
            params = params[1:]
        if add_name in ['append', 'add', 'insert']:
            for key, lazy_value in params:
                result.add(lazy_value)
        elif add_name in ['extend', 'update']:
            for key, lazy_value in params:
                result |= set(lazy_value.infer().iterate())
        return result

    temp_param_add, settings.dynamic_params_for_other_modules = \
        settings.dynamic_params_for_other_modules, False

    is_list = sequence.name.string_name == 'list'
    search_names = (['append', 'extend', 'insert'] if is_list else ['add', 'update'])

    added_types = set()
    for add_name in search_names:
        try:
            possible_names = module_context.tree_node.get_used_names()[add_name]
        except KeyError:
            continue
        else:
            for name in possible_names:
                value_node = context.tree_node
                if not (value_node.start_pos < name.start_pos < value_node.end_pos):
                    continue
                trailer = name.parent
                power = trailer.parent
                trailer_pos = power.children.index(trailer)
                try:
                    execution_trailer = power.children[trailer_pos + 1]
                except IndexError:
                    continue
                else:
                    if execution_trailer.type != 'trailer' \
                            or execution_trailer.children[0] != '(' \
                            or execution_trailer.children[1] == ')':
                        continue

                raise NotImplementedError
                random_context = context.create_context(name)

                with recursion.execution_allowed(context.inference_state, power) as allowed:
                    if allowed:
                        found = infer_call_of_leaf(
                            random_context,
                            name,
                            cut_own_trailer=True
                        )
                        if sequence in found:
                            # The arrays match. Now add the results
                            added_types |= find_additions(
                                random_context,
                                execution_trailer.children[1],
                                add_name
                            )

    # reset settings
    settings.dynamic_params_for_other_modules = temp_param_add
    debug.dbg('Dynamic array result %s' % added_types, color='MAGENTA')
    return added_types


def get_dynamic_array_instance(instance, arguments):
    """Used for set() and list() instances."""
    ai = _ArrayInstance(instance, arguments)
    from jedi.inference import arguments
    return arguments.ValuesArguments([ValueSet([ai])])


class _ArrayInstance(HelperValueMixin):
    """
    Used for the usage of set() and list().
    This is definitely a hack, but a good one :-)
    It makes it possible to use set/list conversions.
    """
    def __init__(self, instance, var_args):
        self.instance = instance
        self.var_args = var_args

    def py__class__(self):
        tuple_, = self.instance.inference_state.builtins_module.py__getattribute__('tuple')
        return tuple_

    def py__iter__(self, valueualized_node=None):
        var_args = self.var_args
        try:
            _, lazy_value = next(var_args.unpack())
        except StopIteration:
            pass
        else:
            for lazy in lazy_value.infer().iterate():
                yield lazy

        from jedi.inference import arguments
        if isinstance(var_args, arguments.TreeArguments):
            additions = _check_array_additions(var_args.context, self.instance)
            for addition in additions:
                yield addition

    def iterate(self, valueualized_node=None, is_async=False):
        return self.py__iter__(valueualized_node)


class Slice(object):
    def __init__(self, value, start, stop, step):
        self._value = value
        self._slice_object = None
        # All of them are either a Precedence or None.
        self._start = start
        self._stop = stop
        self._step = step

    def __getattr__(self, name):
        if self._slice_object is None:
            value = compiled.builtin_from_name(self._value.inference_state, 'slice')
            self._slice_object, = value.execute_with_values()
        return getattr(self._slice_object, name)

    @property
    def obj(self):
        """
        Imitate CompiledObject.obj behavior and return a ``builtin.slice()``
        object.
        """
        def get(element):
            if element is None:
                return None

            result = self._value.infer_node(element)
            if len(result) != 1:
                # For simplicity, we want slices to be clear defined with just
                # one type.  Otherwise we will return an empty slice object.
                raise IndexError

            value, = result
            return get_int_or_none(value)

        try:
            return slice(get(self._start), get(self._stop), get(self._step))
        except IndexError:
            return slice(None, None, None)
