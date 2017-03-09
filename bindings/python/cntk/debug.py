# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

import sys
import numpy as np
from collections import defaultdict

from cntk import cntk_py, user_function

from cntk import output_variable, Constant, Parameter, CloneMethod

from cntk.ops.functions import Function, UserFunction
from cntk.utils.swig_helper import map_if_possible

DEBUG_USAGE = '''\
    Commands:
        n - execute the next node
        n <number> - execute the next <number> nodes
        n f - run until forward pass (like 'n' when already in forward pass)
        n b - run until backward pass (like 'n' when already in backward pass)

        u name - execute until a node with that name is hit
        u <lambda> - execute until the lambda expression is True. Examples:
                     Until a Times node is hit:
                         lambda arg, node: node.op_name == 'Times'
                     Until a node is hit that has 3 dimensions:
                         lambda arg, node: len(node.shape) == 3
                     Until the variance of the input exceeds 1 (np = numpy):
                         lambda arg, node: np.var(arg) > 1

        c - run until end
        p - print input (forward) or root gradients (backward)
        d - drop into a pdb shell
        q - quit\
'''

__doc__ = '''
In order to debug a graph one simply needs to wrap the root node as follows::

    # ... setting up the model in z
    from cntk.debug import debug_model
    z = debug_model(z)

Then, when ``z`` is evaluated or trained (i.e. when either
:meth:`~cntk.ops.functions.Function.forward` or
:meth:`~cntk.ops.functions.Function.backward` is called, you will see the
following command-line interface::

    Forward after Parameter node with uid='Parameter28' shape=[](2,)
    [CNTK forward] >>> help
    %s

    [CNTK backward] >>> n

    ======================================== forward  ========================================
    Forward after Parameter node with uid='Parameter28' shape=[](2,)
    [CNTK forward] >>> n
    Forward after Times node with uid='Times29' shape=[*,*](2,)
    [CNTK forward] >>> n
    ======================================== backward ========================================
    Backward before Times node with uid='Times29' shape=[*,*](2,)
    [CNTK backward] >>> p
    State: None
    Root gradients:
    [[[-0.79412955  0.79412955]]

     [[-0.79412955  0.79412955]]

     [[ 0.20587046 -0.20587045]]

     [[ 0.20587046 -0.20587045]]

     [[ 0.20587046 -0.20587045]]

     [[ 0.20587046 -0.20587045]]

     [[-0.79412955  0.79412955]]

     [[ 0.20587046 -0.20587045]]

     [[ 0.20587039 -0.20587039]]

     [[-0.79412961  0.79412961]]]

At every stop the following information is given:
 * Forward or backward pass
 * Node type (e.g. 'Times')
 * Name if given, otherwise it is omitted
 * uid, which is a unique reference within the graph
 * shape having the format [dynamic axis](static axes). E.g. ``[*,*](2,)``
   means that the node's output has two dynamic axes (batch and sequence) and
   one static axis (2 dimensions)
''' % DEBUG_USAGE


def save_as_legacy_model(root_op, filename):
    '''
    Save the network of ``root_op`` in ``filename``.
    For debugging purposes only, very likely to be deprecated in the future.

    Args:
        root_op (:class:`~cntk.functions.Function`): op of the graph to save
        filename (str): filename to store the model in.
    '''
    cntk_py.save_as_legacy_model(root_op, filename)


class _DebugState(object):

    def __init__(self, all_nodes):
        self.commands = []
        self.last_pass = 'f'
        self.all_nodes = all_nodes
        self.name_to_node = defaultdict(lambda: [])
        for n in self.all_nodes:
            self.name_to_node[n.name].append(n)


class DebugNode(UserFunction):
    '''
    A user function that exposes a command line interface. With that one can
    step through the graph and investigate data, shapes, etc.
    '''
    _commands = []
    _last_pass = 'f'

    def __init__(self, arg, debug_state,
                 in_func=sys.stdin, out_func=sys.stdout,
                 name='DebugNode'):
        if hasattr(arg, 'is_composite') and arg.is_composite:
            arg = arg.root_function

        name += '_after_%s' % arg.uid
        super(DebugNode, self).__init__([arg], as_numpy=True, name=name)
        self.after = arg
        self.debug_state = debug_state

        self._in, self._out = in_func, out_func

    def clone(self, cloned_inputs):
        arg = cloned_inputs[0]
        map_if_possible(arg)
        return DebugNode(arg, self.debug_state)

    # TODO:
    # Breakopint handling
    # u h - until here
    def __wait_for_input(self, prompt):
        understood = False
        while not understood:
            self._out.write(prompt)
            self._out.flush()
            new_input = self._in.readline().strip()
            if not new_input:
                continue

            if len(new_input) == 1 and new_input in 'bcdfp':
                understood = [new_input]
            elif new_input[0] == 'n':
                if len(new_input) > 1:
                    remainder = new_input[1:]
                    try:
                        number = int(remainder)
                        understood = ['n'] * number
                    except ValueError:
                        pass

                    if not understood:
                        if "backward".startswith(remainder):
                            understood = ['nb']
                        elif "forward".startswith(remainder):
                            understood = ['nf']

                else:
                    understood = ['n']

            elif new_input[0] == 'u':
                try:
                    what = new_input[1:].strip()
                    if what.startswith('lambda'):
                        code = eval(what)
                        understood = [code]
                    else:
                        if what in self.debug_state.name_to_node:
                            def code(arg, n):
                                return n.name == what
                            understood = [code]
                        else:
                            self._out.write('Your model does not contain a node with '
                                  'name "%s"\n' % what)
                            self._out.flush()

                except SyntaxError:
                    understood = False

            elif new_input == 'q':
                sys.exit(0)

            if not understood:
                self._out.write(DEBUG_USAGE)
                self._out.flush()

        return understood

    def _print_status(self, current_pass):
        if current_pass != DebugNode._last_pass:
            if current_pass == 'f':
                self._out.write('\n')
                self._out.write('=' * 40 + ' forward  ' + '=' * 40 + '\n')
            else:
                self._out.write('=' * 40 + ' backward ' + '=' * 40 + '\n')
            self._out.flush()

        if current_pass == 'f':
            status = "Forward after"
        else:
            status = "Backward before"

        after = self.after.owner if self.after.is_output else self.after
        self._out.write("%s %s with uid '%s'\n" % (status, str(after), after.uid))
        self._out.flush()

    def forward(self, argument, device=None, outputs_to_retain=None):
        self._print_status('f')

        commands = self.debug_state.commands

        done = False
        while not done:
            if not commands:
                self.debug_state.commands = self.__wait_for_input(
                    '[CNTK forward] >>> ')

            commands = self.debug_state.commands

            next_command = commands[-1]
            if next_command == 'c':
                done = True

            elif isinstance(next_command, str) and next_command.startswith('n'):
                if len(next_command) == 1:
                    commands.pop()
                    done = True
                elif next_command == "nf":
                    commands.pop()
                    done = True
                elif next_command == "nb":
                    done = True

            elif next_command == 'p':
                self._out.write('Input: \n')
                self._out.write(str(argument))
                self._out.write('\n')
                self._out.flush()
                commands.pop()

            elif next_command == 'd':
                commands.pop()
                import pdb
                pdb.set_trace()
                done = True

            elif callable(next_command):
                if next_command(argument, self.after):
                    commands.pop()
                else:
                    done = True

        DebugNode._last_pass = 'f'

        return None, argument

    def backward(self, state, root_gradients):
        self._print_status('b')

        done = False
        while not done:
            if not self.debug_state.commands:
                self.debug_state.commands = self.__wait_for_input(
                    '[CNTK backward] >>> ')

            commands = self.debug_state.commands
            next_command = commands[-1]
            if next_command == 'c':
                done = True

            elif isinstance(next_command, str) and next_command.startswith('n'):
                if len(next_command) == 1:
                    commands.pop()
                    done = True
                elif next_command == "nf":
                    done = True
                elif next_command == "nb":
                    commands.pop()
                    done = True

            elif next_command == 'p':
                self._out.write('State: %s\n' % str(state))
                self._out.write('Root gradients:\n')
                self._out.write(str(root_gradients))
                self._out.write('\n')
                self._out.flush()
                commands.pop()

            elif next_command == 'd':
                import pdb
                pdb.set_trace()
                done = True

            elif callable(next_command):
                if next_command(root_gradients, self.after):
                    commands.pop()
                else:
                    done = True

        self.debug_state.last_pass = 'b'

        return root_gradients

    def infer_outputs(self):
        return [output_variable(self.inputs[0].shape, self.inputs[0].dtype,
                                self.inputs[0].dynamic_axes)]

    def __str__(self):
        return "DebugNode(after=%s)"%str(self.after)


def _nodes_to_debug(model):
    from cntk.graph import depth_first_search

    def node_filter(x):
        if hasattr(x, 'op_name') and x.op_name in ['NoOp']:
            return False
        else:
            return True


    nodes = set(depth_first_search(model, lambda x: True))

    uf_nodes = [n for n in nodes if hasattr(n, 'op_name')
            and n.op_name == 'UserFunction']

    already_covered = [n.inputs[0].owner if n.inputs[0].is_output else
            n.inputs[0] for n in uf_nodes]
    to_remove = [n.uid for n in (already_covered + uf_nodes)]

    return [n for n in nodes if n.uid not in to_remove]


def debug_model(model):
    '''
    Returns a cloned model that has debug nodes inserted everywhere. When the
    graph is evaluated or trained, those nodes will allow to inspect the graph.

    Args:
      model (root node): root node until which the nodes are to be debugged

    Returns:
      a clone of the model that has debugging enabled
    '''
    nodes = _nodes_to_debug(model)
    dbg_state = _DebugState(nodes)

    orig_node_count = len(nodes)
    mod_counter = 1

    # We cannot add the DebugNodes in one clone because the replacements will
    # hide parent nodes.
    while True:
        modifications = {n: user_function(DebugNode(n, dbg_state)) for n in nodes}

        model = model.clone(CloneMethod.share, modifications)
        from cntk.graph import plot

        nodes = _nodes_to_debug(model)
        if len(nodes)==1:
            # last node is the root node
            model = user_function(DebugNode(model, dbg_state))
            break

        if mod_counter > orig_node_count:
            raise ValueError('cannot debug this graph')

    return model
