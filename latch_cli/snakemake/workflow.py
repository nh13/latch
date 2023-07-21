import importlib
import json
import textwrap
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union
from urllib.parse import urlparse

import snakemake
import snakemake.io
import snakemake.jobs
from flytekit.configuration import SerializationSettings
from flytekit.core import constants as _common_constants
from flytekit.core.class_based_resolver import ClassStorageTaskResolver
from flytekit.core.docstring import Docstring
from flytekit.core.interface import Interface, transform_interface_to_typed_interface
from flytekit.core.node import Node
from flytekit.core.promise import NodeOutput, Promise
from flytekit.core.python_auto_container import (
    DefaultTaskResolver,
    PythonAutoContainerTask,
)
from flytekit.core.type_engine import TypeEngine
from flytekit.core.workflow import (
    WorkflowBase,
    WorkflowFailurePolicy,
    WorkflowMetadata,
    WorkflowMetadataDefaults,
)
from flytekit.exceptions import scopes as exception_scopes
from flytekit.models import interface as interface_models
from flytekit.models import literals as literals_models
from flytekit.models import types as type_models
from flytekit.models.core.types import BlobType
from flytekit.models.literals import Blob, BlobMetadata, Literal, LiteralMap, Scalar
from snakemake.dag import DAG
from snakemake.jobs import GroupJob
from snakemake.target_jobs import encode_target_jobs_cli_args
from typing_extensions import TypeAlias, TypedDict

import latch.types.metadata as metadata
from latch.types.directory import LatchDir
from latch.types.file import LatchFile

from ..utils import identifier_suffix_from_str

SnakemakeInputVal: TypeAlias = snakemake.io._IOFile


T = TypeVar("T")


# todo(maximsmol): use a stateful writer that keeps track of indent level
def reindent(x: str, level: int) -> str:
    if x[0] == "\n":
        x = x[1:]
    return textwrap.indent(textwrap.dedent(x), "    " * level)


@dataclass
class JobOutputInfo:
    jobid: str
    output_param_name: str
    type_: Union[LatchFile, LatchDir]


def task_fn_placeholder():
    ...


def variable_name_for_file(file: snakemake.io.AnnotatedString):
    if file[0] == "/":
        return f"a_{identifier_suffix_from_str(file)}"

    return f"r_{identifier_suffix_from_str(file)}"


def variable_name_for_value(
    val: SnakemakeInputVal,
    params: Union[snakemake.io.InputFiles, snakemake.io.OutputFiles, None] = None,
) -> str:
    if params is not None:
        for name, v in params.items():
            if val == v:
                return name

    return variable_name_for_file(val.file)


@dataclass
class RemoteFile:
    local_path: str
    remote_path: str


def snakemake_dag_to_interface(
    dag: DAG, wf_name: str, docstring: Optional[Docstring] = None
) -> (Interface, LiteralMap, List[RemoteFile]):
    outputs: Dict[str, LatchFile] = {}
    for target in dag.targetjobs:
        for desired in target.input:
            param = variable_name_for_value(desired, target.input)

            jobs: list[snakemake.jobs.Job] = dag.file2jobs(desired)
            producer_out: snakemake.io._IOFile = next(
                x for x in jobs[0].output if x == x
            )
            if producer_out.is_directory:
                outputs[param] = LatchDir
            else:
                outputs[param] = LatchFile

    literals: Dict[str, Literal] = {}
    inputs: Dict[str, Tuple[LatchFile, None]] = {}
    return_files: List[RemoteFile] = []
    for job in dag.jobs:
        dep_outputs = []
        for dep, dep_files in dag.dependencies[job].items():
            for o in dep.output:
                if o in dep_files:
                    dep_outputs.append(o)

        for x in job.input:
            if x not in dep_outputs:
                param = variable_name_for_value(x, job.input)
                inputs[param] = (
                    LatchFile,
                    None,
                )
                remote_path = (
                    Path("/.snakemake_latch") / "workflows" / wf_name / "inputs" / x
                )
                remote_url = f"latch://{remote_path}"
                return_files.append(RemoteFile(local_path=x, remote_path=remote_url))
                literals[param] = Literal(
                    scalar=Scalar(
                        blob=Blob(
                            metadata=BlobMetadata(
                                type=BlobType(
                                    format="",
                                    dimensionality=BlobType.BlobDimensionality.SINGLE,
                                )
                            ),
                            uri=remote_url,
                        ),
                    )
                )

    meta = metadata.LatchMetadata(
        display_name=wf_name,
        author=metadata.LatchAuthor(name="Latch Snakemake JIT"),
        parameters={k: metadata.LatchParameter(display_name=k) for k in inputs.keys()},
    )

    return (
        Interface(
            inputs,
            outputs,
            docstring=Docstring(f"{wf_name}\n\nSample Description\n\n" + str(meta)),
        ),
        LiteralMap(literals=literals),
        return_files,
    )


def binding_data_from_python(
    expected_literal_type: type_models.LiteralType,
    t_value: typing.Any,
    t_value_type: Optional[Type] = None,
) -> Optional[literals_models.BindingData]:
    if isinstance(t_value, Promise):
        if not t_value.is_ready:
            return literals_models.BindingData(promise=t_value.ref)


def binding_from_python(
    var_name: str,
    expected_literal_type: type_models.LiteralType,
    t_value: typing.Any,
    t_value_type: Type,
) -> literals_models.Binding:
    binding_data = binding_data_from_python(
        expected_literal_type, t_value, t_value_type
    )
    return literals_models.Binding(var=var_name, binding=binding_data)


def transform_type(
    x: Type, description: Optional[str] = None
) -> interface_models.Variable:
    return interface_models.Variable(
        type=TypeEngine.to_literal_type(x), description=description
    )


def transform_types_in_variable_map(
    variable_map: Dict[str, Type],
    descriptions: Dict[str, str] = {},
) -> Dict[str, interface_models.Variable]:
    res = {}
    if variable_map:
        for k, v in variable_map.items():
            res[k] = transform_type(v, descriptions.get(k, k))
    return res


def interface_to_parameters(
    interface: Optional[Interface],
) -> interface_models.ParameterMap:
    if interface is None or interface.inputs_with_defaults is None:
        return interface_models.ParameterMap({})
    if interface.docstring is None:
        inputs_vars = transform_types_in_variable_map(interface.inputs)
    else:
        inputs_vars = transform_types_in_variable_map(
            interface.inputs, interface.docstring.input_descriptions
        )
    params: Dict[str, interface_models.ParameterMap] = {}
    for k, v in inputs_vars.items():
        val, default = interface.inputs_with_defaults[k]
        required = default is None
        default_lv = None
        if default is not None:
            default_lv = TypeEngine.to_literal(
                None, default, python_type=interface.inputs[k], expected=v.type
            )
        params[k] = interface_models.Parameter(
            var=v, default=default_lv, required=required
        )
    return interface_models.ParameterMap(params)


class JITRegisterWorkflow(WorkflowBase, ClassStorageTaskResolver):
    out_parameter_name = "o0"  # must be "o0"

    def __init__(
        self,
    ):
        assert metadata._snakemake_metadata is not None

        parameter_metadata = metadata._snakemake_metadata.parameters
        metadata._snakemake_metadata.parameters = parameter_metadata
        display_name = metadata._snakemake_metadata.display_name
        name = metadata._snakemake_metadata.name

        docstring = Docstring(
            f"{display_name}\n\nSample Description\n\n"
            + str(metadata._snakemake_metadata)
        )
        native_interface = Interface(
            {k: v.type for k, v in parameter_metadata.items()},
            {self.out_parameter_name: bool},
            docstring=docstring,
        )
        self.parameter_metadata = parameter_metadata
        if metadata._snakemake_metadata.output_dir is not None:
            self.remote_output_url = metadata._snakemake_metadata.output_dir.remote_path
        else:
            self.remote_output_url = None

        workflow_metadata = WorkflowMetadata(
            on_failure=WorkflowFailurePolicy.FAIL_IMMEDIATELY
        )
        name = f"{name}_jit_register"
        workflow_metadata_defaults = WorkflowMetadataDefaults(False)
        super().__init__(
            name=name,
            workflow_metadata=workflow_metadata,
            workflow_metadata_defaults=workflow_metadata_defaults,
            python_interface=native_interface,
        )

    def get_fn_interface(
        self, decorator_name="small_task", fn_name: Optional[str] = None
    ):
        if fn_name is None:
            fn_name = self.name

        params_str = ",\n".join(
            reindent(
                rf"""
                {param}: {t.__name__}
                """,
                1,
            ).rstrip()
            for param, t in self.python_interface.inputs.items()
        )

        return reindent(
            rf"""
            @{decorator_name}
            def {fn_name}(
            __params__
            ) -> bool:
            """,
            0,
        ).replace("__params__", params_str)

    def get_fn_return_stmt(self):
        return reindent(
            rf"""
            return True
            """,
            1,
        )

    def get_fn_code(
        self,
        snakefile_path: str,
        version: str,
        image_name: str,
        account_id: str,
        remote_output_url: Optional[str],
    ):
        task_name = f"{self.name}_task"

        code_block = ""
        code_block += self.get_fn_interface(fn_name=task_name)

        for param, t in self.python_interface.inputs.items():
            if t in (LatchFile, LatchDir):
                code_block += reindent(
                    rf"""
                    {param}_dst_p = Path("{self.parameter_metadata[param].path}")

                    print(f"Downloading {param}: {{{param}.remote_path}}")
                    {param}_p = Path({param}).resolve()
                    print(f"  {{file_name_and_size({param}_p)}}")

                    """,
                    1,
                )

                if t is LatchDir:
                    code_block += reindent(
                        rf"""
                        for x in {param}_p.iterdir():
                            print(f"    {{file_name_and_size(x)}}")

                        """,
                        1,
                    )

                code_block += reindent(
                    rf"""
                    print(f"Moving {param} to {{{param}_dst_p}}")
                    check_exists_and_rename(
                        {param}_p,
                        {param}_dst_p
                    )

                    """,
                    1,
                )
            else:
                raise ValueError(f"Unsupported parameter type {t} for {param}")

        code_block += reindent(
            rf"""
            image_name = "{image_name}"
            image_base_name = image_name.split(":")[0]
            account_id = "{account_id}"
            snakefile = Path("{snakefile_path}")

            lp = LatchPersistence()
            """,
            1,
        )

        code_block += reindent(
            rf"""
            pkg_root = Path(".")

            exec_id_hash = hashlib.sha1()
            exec_id_hash.update(os.environ["FLYTE_INTERNAL_EXECUTION_ID"].encode("utf-8"))
            version = exec_id_hash.hexdigest()[:16]

            wf = extract_snakemake_workflow(pkg_root, snakefile, version)
            wf_name = wf.name
            generate_snakemake_entrypoint(wf, pkg_root, snakefile, {repr(remote_output_url)})

            entrypoint_remote = f"latch:///.snakemake_latch/workflows/{{wf_name}}/entrypoint.py"
            lp.upload("latch_entrypoint.py", entrypoint_remote)
            print(f"latch_entrypoint.py -> {{entrypoint_remote}}")
            """,
            1,
        )

        code_block += reindent(
            r"""
            dockerfile = Path("Dockerfile-dynamic").resolve()
            dockerfile.write_text(
            textwrap.dedent(
                    f'''
                    from 812206152185.dkr.ecr.us-west-2.amazonaws.com/{image_name}

                    copy latch_entrypoint.py /root/latch_entrypoint.py
                    '''
                )
            )
            new_image_name = f"{image_name}-{version}"

            os.mkdir("/root/.ssh")
            ssh_key_path = Path("/root/.ssh/id_rsa")
            cmd = ["ssh-keygen", "-f", ssh_key_path, "-N", "", "-q"]
            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError as e:
                raise ValueError(
                    "There was a problem creating temporary SSH credentials. Please ensure"
                    " that `ssh-keygen` is installed and available in your PATH."
                ) from e
            os.chmod(ssh_key_path, 0o700)

            token = os.environ.get("FLYTE_INTERNAL_EXECUTION_ID", "")
            headers = {
                "Authorization": f"Latch-Execution-Token {token}",
            }

            ssh_public_key_path = Path("/root/.ssh/id_rsa.pub")
            response = tinyrequests.post(
                config.api.centromere.provision,
                headers=headers,
                json={
                    "public_key": ssh_public_key_path.read_text().strip(),
                },
            )

            resp = response.json()
            try:
                public_ip = resp["ip"]
                username = resp["username"]
            except KeyError as e:
                raise ValueError(
                    f"Malformed response from request for centromere login: {resp}"
                ) from e


            subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", f"{username}@{public_ip}", "uptime"])
            dkr_client = _construct_dkr_client(ssh_host=f"ssh://{username}@{public_ip}")

            data = {"pkg_name": new_image_name.split(":")[0], "ws_account_id": account_id}
            response = requests.post(config.api.workflow.upload_image, headers=headers, json=data)

            try:
                response = response.json()
                access_key = response["tmp_access_key"]
                secret_key = response["tmp_secret_key"]
                session_token = response["tmp_session_token"]
            except KeyError as err:
                raise ValueError(f"malformed response on image upload: {response}") from err

            try:
                client = boto3.session.Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    aws_session_token=session_token,
                    region_name="us-west-2",
                ).client("ecr")
                token = client.get_authorization_token()["authorizationData"][0][
                    "authorizationToken"
                ]
            except Exception as err:
                raise ValueError(
                    f"unable to retreive an ecr login token for user {account_id}"
                ) from err

            user, password = base64.b64decode(token).decode("utf-8").split(":")
            dkr_client.login(
                username=user,
                password=password,
                registry=config.dkr_repo,
            )

            image_build_logs = dkr_client.build(
                path=str(pkg_root),
                dockerfile=str(dockerfile),
                buildargs={"tag": f"{config.dkr_repo}/{new_image_name}"},
                tag=f"{config.dkr_repo}/{new_image_name}",
                decode=True,
            )
            print_and_write_build_logs(image_build_logs, new_image_name, pkg_root)

            upload_image_logs = dkr_client.push(
                repository=f"{config.dkr_repo}/{new_image_name}",
                stream=True,
                decode=True,
            )
            print_upload_logs(upload_image_logs, new_image_name)

            temp_dir = tempfile.TemporaryDirectory()
            with Path(temp_dir.name).resolve() as td:
                serialize_snakemake(wf, td, new_image_name, config.dkr_repo)

                protos = _recursive_list(td)
                reg_resp = register_serialized_pkg(protos, None, version, account_id)
                _print_reg_resp(reg_resp, new_image_name)

            wf_spec_remote = f"latch:///.snakemake_latch/workflows/{wf_name}/spec.json"
            lp.upload("wf_spec.json", wf_spec_remote)
            print(f"wf_spec.json -> {wf_spec_remote}")


            class _WorkflowInfoNode(TypedDict):
                id: str


            nodes: Optional[List[_WorkflowInfoNode]] = None
            while not nodes:
                time.sleep(1)
                nodes = execute(
                    gql.gql('''
                    query workflowQuery($name: String, $ownerId: BigInt, $version: String) {
                    workflowInfos(condition: { name: $name, ownerId: $ownerId, version: $version}) {
                        nodes {
                            id
                        }
                    }
                    }
                    '''),
                    {"name": wf_name, "version": version, "ownerId": account_id},
                )["workflowInfos"]["nodes"]

            if len(nodes) > 1:
                raise ValueError(
                    "Invariant violated - more than one workflow identified for unique combination"
                    " of {wf_name}, {version}, {account_id}"
                )

            print(nodes)

            for file in wf.return_files:
                print(f"Uploading {file.local_path} -> {file.remote_path}")
                lp.upload(file.local_path, file.remote_path)

            wf_id = nodes[0]["id"]
            params = json.loads(gpjson.MessageToJson(wf.literal_map.to_flyte_idl()))["literals"]

            _interface_request = {
                "workflow_id": wf_id,
                "params": params,
            }

            response = requests.post(urljoin(config.nucleus_url, "/api/create-execution"), headers=headers, json=_interface_request)
            print(response.json())
            """,
            1,
        )
        code_block += self.get_fn_return_stmt()
        return code_block


class SnakemakeWorkflow(WorkflowBase, ClassStorageTaskResolver):
    def __init__(
        self,
        dag: DAG,
        version: Optional[str] = None,
    ):
        name = metadata._snakemake_metadata.name

        native_interface, literal_map, return_files = snakemake_dag_to_interface(
            dag, name, None
        )
        self.literal_map = literal_map
        self.return_files = return_files
        self._input_parameters = None
        self._dag = dag
        self.snakemake_tasks = []

        workflow_metadata = WorkflowMetadata(
            on_failure=WorkflowFailurePolicy.FAIL_IMMEDIATELY
        )
        workflow_metadata_defaults = WorkflowMetadataDefaults(False)
        super().__init__(
            name=name,
            workflow_metadata=workflow_metadata,
            workflow_metadata_defaults=workflow_metadata_defaults,
            python_interface=native_interface,
        )

    def compile(self, **kwargs):
        self._input_parameters = interface_to_parameters(self.python_interface)

        GLOBAL_START_NODE = Node(
            id=_common_constants.GLOBAL_INPUT_NODE_ID,
            metadata=None,
            bindings=[],
            upstream_nodes=[],
            flyte_entity=None,
        )

        node_map: Dict[str, Node] = {}

        target_files = [x for job in self._dag.targetjobs for x in job.input]

        for layer in self._dag.toposorted():
            for job in layer:
                assert isinstance(job, snakemake.jobs.Job)
                is_target = False

                if job in self._dag.targetjobs:
                    continue

                target_file_for_output_param: Dict[str, str] = {}
                target_file_for_input_param: Dict[str, str] = {}

                python_outputs: Dict[str, Union[LatchFile, LatchDir]] = {}
                for x in job.output:
                    assert isinstance(x, SnakemakeInputVal)

                    if x in target_files:
                        is_target = True
                    param = variable_name_for_value(x, job.output)
                    target_file_for_output_param[param] = x

                    if x.is_directory:
                        python_outputs[param] = LatchDir
                    else:
                        python_outputs[param] = LatchFile

                dep_outputs: dict[SnakemakeInputVal, JobOutputInfo] = {}
                for dep, dep_files in self._dag.dependencies[job].items():
                    for o in dep.output:
                        if o in dep_files:
                            assert isinstance(o, SnakemakeInputVal)

                            dep_outputs[o] = JobOutputInfo(
                                jobid=dep.jobid,
                                output_param_name=variable_name_for_value(
                                    o, dep.output
                                ),
                                type_=LatchDir if o.is_directory else LatchFile,
                            )

                python_inputs: Dict[str, Union[LatchFile, LatchDir]] = {}
                promise_map: Dict[str, JobOutputInfo] = {}
                for x in job.input:
                    param = variable_name_for_value(x, job.input)
                    target_file_for_input_param[param] = x

                    dep_out = dep_outputs.get(x)

                    python_inputs[param] = LatchFile

                    if dep_out is not None:
                        python_inputs[param] = dep_out.type_
                        promise_map[param] = dep_out

                interface = Interface(python_inputs, python_outputs, docstring=None)
                task = SnakemakeJobTask(
                    wf=self,
                    job=job,
                    inputs=python_inputs,
                    outputs=python_outputs,
                    target_file_for_input_param=target_file_for_input_param,
                    target_file_for_output_param=target_file_for_output_param,
                    is_target=is_target,
                    interface=interface,
                )
                self.snakemake_tasks.append(task)

                typed_interface = transform_interface_to_typed_interface(interface)
                assert typed_interface is not None

                bindings: List[literals_models.Binding] = []
                for k in interface.inputs:
                    var = typed_interface.inputs[k]
                    if var.description in promise_map:
                        job_output_info = promise_map[var.description]
                        promise_to_bind = Promise(
                            var=k,
                            val=NodeOutput(
                                node=node_map[job_output_info.jobid],
                                var=job_output_info.output_param_name,
                            ),
                        )
                    else:
                        promise_to_bind = Promise(
                            var=k,
                            val=NodeOutput(node=GLOBAL_START_NODE, var=k),
                        )
                    bindings.append(
                        binding_from_python(
                            var_name=k,
                            expected_literal_type=var.type,
                            t_value=promise_to_bind,
                            t_value_type=interface.inputs[k],
                        )
                    )

                upstream_nodes = []
                for x in self._dag.dependencies[job].keys():
                    if x.jobid in node_map:
                        upstream_nodes.append(node_map[x.jobid])

                node = Node(
                    id=f"n{job.jobid}",
                    metadata=task.construct_node_metadata(),
                    bindings=sorted(bindings, key=lambda b: b.var),
                    upstream_nodes=upstream_nodes,
                    flyte_entity=task,
                )
                node_map[job.jobid] = node

        bindings: List[literals_models.Binding] = []
        for i, out in enumerate(self.interface.outputs.keys()):
            upstream_id, upstream_var = self.find_upstream_node_matching_output_var(out)
            promise_to_bind = Promise(
                var=out,
                val=NodeOutput(node=node_map[upstream_id], var=upstream_var),
            )
            t = self.python_interface.outputs[out]
            b = binding_from_python(
                out,
                self.interface.outputs[out].type,
                promise_to_bind,
                t,
            )
            bindings.append(b)

        self._nodes = list(node_map.values())
        self._output_bindings = bindings

    def find_upstream_node_matching_output_var(self, out_var: str):
        for j in self._dag.targetjobs:
            for depen, files in self._dag.dependencies[j].items():
                for f in files:
                    if variable_name_for_file(f) == out_var:
                        return depen.jobid, variable_name_for_value(f, depen.output)

    def execute(self, **kwargs):
        return exception_scopes.user_entry_point(self._workflow_function)(**kwargs)


def build_jit_register_wrapper() -> JITRegisterWorkflow:
    wrapper_wf = JITRegisterWorkflow()
    out_parameter_name = wrapper_wf.out_parameter_name

    python_interface = wrapper_wf.python_interface
    wrapper_wf._input_parameters = interface_to_parameters(python_interface)

    GLOBAL_START_NODE = Node(
        id=_common_constants.GLOBAL_INPUT_NODE_ID,
        metadata=None,
        bindings=[],
        upstream_nodes=[],
        flyte_entity=None,
    )
    task_interface = Interface(
        python_interface.inputs, python_interface.outputs, docstring=None
    )
    task = PythonAutoContainerTask[T](
        name=f"{wrapper_wf.name}_task",
        task_type="python-task",
        interface=task_interface,
        task_config=None,
        task_resolver=JITRegisterWorkflowResolver(),
    )

    task_bindings: List[literals_models.Binding] = []
    typed_interface = transform_interface_to_typed_interface(python_interface)
    for k in python_interface.inputs:
        var = typed_interface.inputs[k]
        promise_to_bind = Promise(
            var=k,
            val=NodeOutput(node=GLOBAL_START_NODE, var=k),
        )
        task_bindings.append(
            binding_from_python(
                var_name=k,
                expected_literal_type=var.type,
                t_value=promise_to_bind,
                t_value_type=python_interface.inputs[k],
            )
        )
    task_node = Node(
        id="n0",
        metadata=task.construct_node_metadata(),
        bindings=sorted(task_bindings, key=lambda b: b.var),
        upstream_nodes=[],
        flyte_entity=task,
    )

    promise_to_bind = Promise(
        var=out_parameter_name,
        val=NodeOutput(node=task_node, var=out_parameter_name),
    )
    t = python_interface.outputs[out_parameter_name]
    output_binding = binding_from_python(
        out_parameter_name,
        bool,
        promise_to_bind,
        t,
    )

    wrapper_wf._nodes = [task_node]
    wrapper_wf._output_bindings = [output_binding]
    return wrapper_wf


class AnnotatedStrJson(TypedDict):
    value: str
    flags: dict[str, bool]


MaybeAnnotatedStrJson: TypeAlias = Union[str, AnnotatedStrJson]


def annotated_str_to_json(
    x: Union[str, snakemake.io._IOFile, snakemake.io.AnnotatedString]
) -> MaybeAnnotatedStrJson:
    if not isinstance(x, (snakemake.io.AnnotatedString, snakemake.io._IOFile)):
        return x

    return {"value": str(x), "flags": dict(x.flags.items())}


IONamedListItem = Union[MaybeAnnotatedStrJson, list[MaybeAnnotatedStrJson]]


class NamedListJson(TypedDict):
    positional: list[IONamedListItem]
    keyword: dict[str, IONamedListItem]


def named_list_to_json(xs: snakemake.io.Namedlist) -> NamedListJson:
    named: dict[str, IONamedListItem] = {}
    for k, vs in xs.items():
        if not isinstance(vs, list):
            named[k] = annotated_str_to_json(vs)
            continue

        named[k] = [annotated_str_to_json(v) for v in vs]

    named_values = set()
    for vs in named.values():
        if not isinstance(vs, list):
            vs = [vs]

        for v in vs:
            if not isinstance(v, str):
                v = v["value"]
            named_values.add(v)

    unnamed: list[IONamedListItem] = []
    for vs in xs:
        if not isinstance(vs, list):
            vs = [vs]

        for v in vs:
            obj = annotated_str_to_json(v)

            rendered = obj
            if not isinstance(rendered, str):
                rendered = rendered["value"]
            if rendered in named_values:
                continue

            unnamed.append(obj)

    return {"positional": unnamed, "keyword": named}


class SnakemakeJobTask(PythonAutoContainerTask[T]):
    def __init__(
        self,
        wf: SnakemakeWorkflow,
        job: snakemake.jobs.Job,
        inputs: Dict[str, Union[Type[LatchFile], Type[LatchDir]]],
        outputs: Dict[str, Union[Type[LatchFile], Type[LatchDir]]],
        target_file_for_input_param: Dict[str, str],
        target_file_for_output_param: Dict[str, str],
        is_target: bool,
        interface: Interface,
        task_type="python-task",
    ):
        name = f"{job.name}_{job.jobid}"

        self.wf = wf
        self.job = job
        self._is_target = is_target
        self._python_inputs = inputs
        self._python_outputs = outputs
        self._target_file_for_input_param = target_file_for_input_param
        self._target_file_for_output_param = target_file_for_output_param

        self._task_function = task_fn_placeholder

        super().__init__(
            task_type=task_type,
            name=name,
            interface=interface,
            task_config=None,
            task_resolver=SnakemakeJobTaskResolver(),
        )

    def get_fn_interface(self):
        res = ""

        params_str = ",\n".join(
            reindent(
                rf"""
                {param}: {t.__name__}
                """,
                1,
            ).rstrip()
            for param, t in self._python_inputs.items()
        )

        outputs_str = "None:"
        if len(self._python_outputs.items()) > 0:
            output_fields = "\n".join(
                reindent(
                    rf"""
                    {param}: {t.__name__}
                    """,
                    1,
                ).rstrip()
                for param, t in self._python_outputs.items()
            )

            res += reindent(
                rf"""
                class Res{self.name}(NamedTuple):
                __output_fields__

                """,
                0,
            ).replace("__output_fields__", output_fields)
            outputs_str = f"Res{self.name}:"

        res += (
            reindent(
                rf"""
                @small_task(cache=True)
                def {self.name}(
                __params__
                ) -> __outputs__
                """,
                0,
            )
            .replace("__params__", params_str)
            .replace("__outputs__", outputs_str)
        )
        return res

    def get_fn_return_stmt(self, remote_output_url: Optional[str] = None):
        print_outs: list[str] = []
        results: list[str] = []
        for out_name, out_type in self._python_outputs.items():
            target_path = self._target_file_for_output_param[out_name]

            print_outs.append(
                reindent(
                    rf"""
                    print(f'  {out_name}={{file_name_and_size(Path("{target_path}"))}}')
                    """,
                    1,
                )
            )

            if not self._is_target:
                results.append(
                    reindent(
                        rf"""
                        {out_name}={out_type.__name__}("{target_path}")
                        """,
                        2,
                    ).rstrip()
                )
                continue

            if remote_output_url is None:
                remote_path = Path("/Snakemake Outputs") / self.wf.name / target_path
            else:
                remote_path = Path(urlparse(remote_output_url).path) / target_path

            results.append(
                reindent(
                    rf"""
                    {out_name}={out_type.__name__}("{target_path}", "latch://{remote_path}")
                    """,
                    2,
                ).rstrip()
            )

        print_out_str = "\n".join(print_outs)
        return_str = ",\n".join(results)

        return (
            reindent(
                rf"""
                    print("Uploading results:")
                __print_out__

                    return Res{self.name}(
                __return_str__
                    )
            """,
                0,
            )
            .replace("__print_out__", print_out_str)
            .replace("__return_str__", return_str)
        )

    def get_fn_code(
        self, snakefile_path_in_container: str, remote_output_url: Optional[str] = None
    ):
        code_block = ""
        code_block += self.get_fn_interface()

        for param, t in self._python_inputs.items():
            if t == LatchFile:
                code_block += reindent(
                    rf"""
                    {param}_dst_p = Path("{self._target_file_for_input_param[param]}")

                    print(f"Downloading {param}: {{{param}.remote_path}}")
                    {param}_p = Path({param}).resolve()
                    print(f"  {{file_name_and_size({param}_p)}}")

                    """,
                    1,
                )

                code_block += reindent(
                    rf"""
                    print(f"Moving {param} to {{{param}_dst_p}}")
                    check_exists_and_rename(
                        {param}_p,
                        {param}_dst_p
                    )
                    """,
                    1,
                )

        snakemake_args = [
            "-m",
            "latch_cli.snakemake.single_task_snakemake",
            "-s",
            snakefile_path_in_container,
            "--target-jobs",
            *encode_target_jobs_cli_args(self.job.get_target_spec()),
            "--allowed-rules",
            *self.job.rules,
            "--local-groupid",
            str(self.job.jobid),
            "--cores",
            str(self.job.threads),
            # "--print-compilation",
        ]
        if not self.job.is_group():
            snakemake_args.append("--force-use-threads")

        excluded = {"_nodes", "_cores", "tmpdir"}
        allowed_resources = list(
            filter(lambda x: x[0] not in excluded, self.job.resources.items())
        )
        if len(allowed_resources) > 0:
            snakemake_args.append("--resources")
            for resource, value in allowed_resources:
                snakemake_args.append(f"{resource}={value}")

        jobs = [self.job]
        if isinstance(self.job, GroupJob):
            jobs = self.job.jobs

        snakemake_data = {
            "rules": {},
            "outputs": self.job.output,
        }

        for job in jobs:
            rule = job.rule

            snakemake_data["rules"][job.rule.name] = {
                "inputs": named_list_to_json(job.rule.input),
                "outputs": named_list_to_json(job.rule.output),
                "params": named_list_to_json(job.rule.params),
            }

        if remote_output_url is None:
            remote_path = Path("/Snakemake Outputs") / self.wf.name
        else:
            remote_path = Path(urlparse(remote_output_url).path)

        log_files = self.job.log if self.job.log is not None else []

        code_block += reindent(
            rf"""
            lp = LatchPersistence()
            compiled = Path("compiled.py")
            print("Saving compiled Snakemake script")
            with compiled.open("w") as f:
                try:
                    subprocess.run(
                        [sys.executable,{','.join(repr(x) for x in [*snakemake_args, "--print-compilation"])}],
                        check=True,
                        env={{
                            **os.environ,
                            "LATCH_SNAKEMAKE_DATA": {repr(json.dumps(snakemake_data))}
                        }},
                        stdout=f
                    )
                except CalledProcessError:
                    print("  Failed")
                except Exception:
                    traceback.print_exc()
            lp.upload(compiled, "latch:///.snakemake_latch/workflows/{self.wf.name}/compiled_tasks/{self.name}.py")

            print("\n\n\nRunning snakemake task\n")
            try:
                log_files = {repr(log_files)}
                try:
                    tail = None
                    if len(log_files) == 1:
                        log = Path(log_files[0])
                        print(f"Tailing the only log file: {{log}}")
                        tail = subprocess.Popen(["tail", "--follow", log])

                    print("\n\n\n")
                    try:
                        subprocess.run(
                            [sys.executable,{','.join(repr(x) for x in snakemake_args)}],
                            check=True,
                            env={{
                                **os.environ,
                                "LATCH_SNAKEMAKE_DATA": {repr(json.dumps(snakemake_data))}
                            }}
                        )
                    finally:
                        if tail is not None:
                            tail.send_signal(SIGINT)
                            try:
                                tail.wait(1)
                            except subprocess.TimeoutExpired:
                                tail.kill()

                            tail.wait()
                            if tail.returncode != 0:
                                print(f"\n\n\n[!] Log file tail died with code {{tail.returncode}}")

                    print("\n\n\nDone\n\n\n")
                except Exception as e:
                    print("\n\n\n[!] Failed\n\n\n")
                    raise e
                finally:
                    print("Uploading logs:")
                    for x in log_files:
                        local = Path(x)
                        remote = f"latch://{remote_path}/{{str(local).removeprefix('/')}}"
                        print(f"  {{file_name_and_size(local)}} -> {{remote}}")
                        if not local.exists():
                            print("  Does not exist")
                            continue

                        lp.upload(local, remote)
                        print("    Done")

                    benchmark_file = {repr(self.job.benchmark)}
                    if benchmark_file is not None:
                        print("\nUploading benchmark:")

                        local = Path(benchmark_file)
                        if local.exists():
                            print(local.read_text())

                            remote = f"latch://{remote_path}/{{str(local).removeprefix('/')}}"
                            print(f"  {{file_name_and_size(local)}} -> {{remote}}")
                            lp.upload(local, remote)
                            print("    Done")
                        else:
                            print("  Does not exist")

            except CalledProcessError:
                ignored_paths = {{".cache"}}
                ignored_names = {{".git", ".latch", "__pycache__"}}

                print("Recursive directory listing:")
                stack = [(Path("."), 0)]
                while len(stack) > 0:
                    cur, indent = stack.pop()
                    print("  " * indent + cur.name)

                    if cur.is_dir():
                        if cur.name in ignored_names or str(cur) in ignored_paths:
                            print("  " * indent + "  ...")
                            continue

                        for x in cur.iterdir():
                            stack.append((x, indent + 1))

                sys.exit(1)

            """,
            1,
        )

        code_block += self.get_fn_return_stmt(remote_output_url=remote_output_url)
        return code_block

    @property
    def dockerfile_path(self) -> Path:
        return self._dockerfile_path

    @property
    def task_function(self):
        return self._task_function

    def execute(self, **kwargs) -> Any:
        return exception_scopes.user_entry_point(self._task_function)(**kwargs)


class SnakemakeJobTaskResolver(DefaultTaskResolver):
    @property
    def location(self) -> str:
        return "flytekit.core.python_auto_container.default_task_resolver"

    def loader_args(
        self, settings: SerializationSettings, task: SnakemakeJobTask
    ) -> List[str]:
        return ["task-module", "latch_entrypoint", "task-name", task.name]

    def load_task(self, loader_args: List[str]) -> PythonAutoContainerTask:
        _, task_module, _, task_name, *_ = loader_args

        task_module = importlib.import_module(task_module)

        task_def = getattr(task_module, task_name)
        return task_def


class JITRegisterWorkflowResolver(DefaultTaskResolver):
    @property
    def location(self) -> str:
        return "flytekit.core.python_auto_container.default_task_resolver"

    def loader_args(
        self, settings: SerializationSettings, task: PythonAutoContainerTask[T]
    ) -> List[str]:
        return ["task-module", "snakemake_jit_entrypoint", "task-name", task.name]

    def load_task(self, loader_args: List[str]) -> PythonAutoContainerTask:
        _, task_module, _, task_name, *_ = loader_args

        task_module = importlib.import_module(task_module)

        task_def = getattr(task_module, task_name)
        return task_def
