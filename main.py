from __future__ import annotations
from typing import Any, Tuple, TypedDict

from Mir import LanguageServer, mir, server_for_view, get_view_uri, deno, LoaderInStatusBar, PackageStorage, command, file_name_to_uri
from Mir.libs.lsp.server import mir_logger
from Mir.types.lsp import DocumentUri, FormattingOptions, TextEdit, WorkspaceFolder
import sublime_aio
from sublime_lib import ResourcePath
import sublime
from os import path
from urllib.parse import quote


server_storage = PackageStorage(tag='0.0.1')
server_storage.copy("./language-server")
server_path = server_storage / "language-server" / "out" / "node" / "jsonServerMain.js"

async def package_storage_setup():
    if server_path.exists():
        return
    await deno.setup()
    server_storage.copy("./language-server")
    with LoaderInStatusBar(f'installing json'):
        await command([deno.path, "install"], cwd=str(server_storage / "language-server"))

class JsonServer(LanguageServer):
    name='json'
    activation_events={
        'selector': 'source.json',
    }
    settings_file="Mir-json.sublime-settings"

    async def activate(self):
        # setup runtime and install dependencies
        await package_storage_setup()

        user_schemas = resolve_file_paths(
          self.initialize_params['workspaceFolders'],
          self.settings.get('json.userSchemas') or []
        )

        schema_uri_to_content: dict= {}
        schema_list = []
        package_name = __package__
        for schema in ['json-schemas_extra.json', 'json-schemas.json']:
            path = 'Packages/{}/{}'.format(package_name, schema)
            schemas = parse_schema(ResourcePath(path)) or []
            for schema in schemas:
                file_matches = schema.get('fileMatch')
                if file_matches:
                    schema['fileMatch'] = [quote(fm, safe="/*!") for fm in file_matches]
                schema_list.append(schema)

        resources = ResourcePath.glob_resources('sublime-package.json')
        for resource in resources:
            schema: dict | None = None
            try:
                schema = sublime.decode_value(resource.read_text())
            except Exception as e:
                mir_logger.error(f'Error parsing sublime-package.json {resource.name}', exc_info=e)
                continue
            if not schema:
                continue
            sublime_package_settings = schema.get('contributions', {}).get('settings')
            for s in sublime_package_settings:
                file_patterns = s.get('file_patterns', [])
                schema_content = s.get('schema')
                uri = schema_content.get('$id')
                schema_uri_to_content[uri] = sublime.encode_value(schema_content, pretty=False)
                schema_list.append({'fileMatch':  [quote(fm, safe="/*!") for fm in file_patterns], 'uri': uri})

        def handle_vscode_content(params: Tuple[str]):
            uri = params[0]
            if uri in schema_uri_to_content:
                return schema_uri_to_content[uri]
            if uri.startswith('sublime://'):
                schema_path = uri.replace('sublime://', '')
                schema_components = schema_path.split('/')
                domain = schema_components[0]
                if domain == 'schemas':
                    # Internal schema - 1:1 schema path to file path mapping.
                    schema_path = 'Packages/{}/{}.json'.format(package_name, schema_path)
                    content =  sublime.encode_value(sublime.decode_value(ResourcePath(schema_path).read_text()), pretty=False)
                    schema_uri_to_content[uri] = content
                    return content
            print('{}: Unknown schema URI "{}"'.format(package_name, uri))
            return None

        self.on_request('vscode/content', handle_vscode_content)

        await self.initialize({
            'communication_channel': 'stdio',
            # --unstable-detect-cjs - is required to avoid the following Deno output warning
            'command': [deno.path,  'run', '-A',  '--unstable-detect-cjs', server_path, '--stdio'],
            'initialization_options': self.settings.get('json.initialization_options'),
        })
        self.send_notification('json/schemaAssociations', [get_schemas() + schema_list + user_schemas ])


mir.commands.register_command('json.sort', 'mir_json_sort_document')

class JsonSortDocumentParams(TypedDict):
    uri: DocumentUri
    options: FormattingOptions


class Schema(TypedDict):
    fileMatch: list[str]
    uri: str


def parse_schema(resource: ResourcePath) -> Any:
    try:
        return sublime.decode_value(resource.read_text())
    except Exception:
        print('Failed parsing schema "{}"'.format(resource.file_path()))
        return None


class mir_json_sort_document_command(sublime_aio.ViewCommand):
    async def run(self, arguments=None) -> None:
        server = server_for_view('json', self.view)
        if server is None:
            return
        params: JsonSortDocumentParams = {
            'uri': get_view_uri(self.view),
            'options': formatting_options(self.view.settings()),
        }
        req = server.send_request('json/sort', params)
        text_edits: list[TextEdit] = await req.result
        self.view.run_command('mir_apply_text_edits', {
            'text_edits': text_edits
        })


def formatting_options(settings: sublime.Settings) -> dict[str, Any]:
    # Build 4085 allows "trim_trailing_white_space_on_save" to be a string so we have to account for that in a
    # backwards-compatible way.
    trim_trailing_white_space = settings.get("trim_trailing_white_space_on_save") not in (False, None, "none")
    return {
        # Size of a tab in spaces.
        "tabSize": settings.get("tab_size", 4),
        # Prefer spaces over tabs.
        "insertSpaces": settings.get("translate_tabs_to_spaces", False),
        # Trim trailing whitespace on a line. (since 3.15)
        "trimTrailingWhitespace": trim_trailing_white_space,
        # Insert a newline character at the end of the file if one does not exist. (since 3.15)
        "insertFinalNewline": settings.get("ensure_newline_at_eof_on_save", False),
        # Trim all newlines after the final newline at the end of the file. (sine 3.15)
        "trimFinalNewlines": settings.get("ensure_newline_at_eof_on_save", False)
    }

def resolve_file_paths(workspace_folders: list[WorkspaceFolder], schemas: list[Schema]) -> list[Schema]:
    if not workspace_folders:
        return schemas
    for schema in schemas:
        # Filesystem paths are resolved relative to the first workspace folder.
        if schema['uri'].startswith(('.', '/')):
            absolute_path = path.normpath(path.join(workspace_folders[0].path, schema['uri']))
            schema['uri'] = file_name_to_uri(absolute_path)
    return schemas

def get_schemas():
    return [
      {
        "uri": "https://json.schemastore.org/base.json"
      },
      {
        "uri": "https://json.schemastore.org/base-04.json"
      },
      {
        "fileMatch": [
          "/.awc.json",
          "/.awc.jsonc",
          "/.awc"
        ],
        "uri": "https://json.schemastore.org/anywork-ac-1.1.json"
      },
      {
        "fileMatch": [
          "/.adonisrc.json"
        ],
        "uri": "https://raw.githubusercontent.com/adonisjs/application/master/adonisrc.schema.json"
      },
      {
        "fileMatch": [
          "/.agripparc.json",
          "/agripparc.json"
        ],
        "uri": "https://json.schemastore.org/agripparc-1.4.json"
      },
      {
        "fileMatch": [
          "/.aiproj.json"
        ],
        "uri": "https://json.schemastore.org/aiproj-1.1.json"
      },
      {
        "fileMatch": [
          "/*.task.json"
        ],
        "uri": "https://api.airplane.dev/v0/schemas/task.json"
      },
      {
        "fileMatch": [
          "/angular.json"
        ],
        "uri": "https://raw.githubusercontent.com/angular/angular-cli/master/packages/angular/cli/lib/config/workspace-schema.json"
      },
      {
        "fileMatch": [
          "/.angular-cli.json",
          "/angular-cli.json"
        ],
        "uri": "https://raw.githubusercontent.com/angular/angular-cli/v10.1.6/packages/angular/cli/lib/config/schema.json"
      },
      {
        "fileMatch": [
          "/.ansible-lint"
        ],
        "uri": "https://raw.githubusercontent.com/ansible/ansible-lint/main/src/ansiblelint/schemas/ansible-lint-config.json"
      },
      {
        "fileMatch": [
          "/.ansible-navigator.json",
          "/ansible-navigator.json"
        ],
        "uri": "https://raw.githubusercontent.com/ansible/ansible-navigator/main/src/ansible_navigator/data/ansible-navigator.json"
      },
      {
        "fileMatch": [
          "/apple-app-site-association"
        ],
        "uri": "https://json.schemastore.org/apple-app-site-association.json"
      },
      {
        "fileMatch": [
          "/appsscript.json"
        ],
        "uri": "https://json.schemastore.org/appsscript.json"
      },
      {
        "fileMatch": [
          "/appsettings.json",
          "/appsettings.*.json"
        ],
        "uri": "https://json.schemastore.org/appsettings.json"
      },
      {
        "fileMatch": [
          "/arc.json"
        ],
        "uri": "https://raw.githubusercontent.com/architect/parser/v2.3.0/arc-schema.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/argoproj/argo-events/master/api/jsonschema/schema.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/argoproj/argo-workflows/master/api/jsonschema/schema.json"
      },
      {
        "fileMatch": [
          "/asconfig.json"
        ],
        "uri": "https://json.schemastore.org/asconfig-schema.json"
      },
      {
        "fileMatch": [
          "/*asyncapi*.json"
        ],
        "uri": "https://www.asyncapi.com/schema-store/all.schema-store.json"
      },
      {
        "fileMatch": [
          "/.asyncapi-tool"
        ],
        "uri": "https://raw.githubusercontent.com/asyncapi/website/master/scripts/tools/tools-schema.json"
      },
      {
        "fileMatch": [
          "/*.avsc"
        ],
        "uri": "https://json.schemastore.org/avro-avsc.json"
      },
      {
        "fileMatch": [
          "/*.importmanifest.json"
        ],
        "uri": "https://json.schemastore.org/azure-deviceupdate-import-manifest-5.0.json"
      },
      {
        "fileMatch": [
          "/*.updatemanifest.json"
        ],
        "uri": "https://json.schemastore.org/azure-deviceupdate-update-manifest-5.json"
      },
      {
        "uri": "https://json.schemastore.org/azure-devops-extension-manifest-1.0.json"
      },
      {
        "uri": "https://json.schemastore.org/azure-iot-edgeagent-deployment-1.1.json"
      },
      {
        "uri": "https://json.schemastore.org/azure-iot-edgehub-deployment-1.2.json"
      },
      {
        "uri": "https://json.schemastore.org/azure-iot-edge-deployment-2.0.json"
      },
      {
        "fileMatch": [
          "/deployment.template.json",
          "/deployment.debug.template.json"
        ],
        "uri": "https://json.schemastore.org/azure-iot-edge-deployment-template-4.0.json"
      },
      {
        "fileMatch": [
          "/cv.json",
          "/*.cv.json"
        ],
        "uri": "https://raw.githubusercontent.com/hexagonkt/codecv/master/cv.schema.json"
      },
      {
        "fileMatch": [
          "/lobe-agent.json"
        ],
        "uri": "https://chat-agents.lobehub.com/schema/lobeAgentSchema_v1.json"
      },
      {
        "fileMatch": [
          "/manifest.json"
        ],
        "uri": "https://json.schemastore.org/foxx-manifest.json"
      },
      {
        "fileMatch": [
          "/flagd.json",
          "/*.flagd.json"
        ],
        "uri": "https://raw.githubusercontent.com/open-feature/schemas/main/json/flagd-definitions.json"
      },
      {
        "fileMatch": [
          "/*.freifunk-api.json"
        ],
        "uri": "https://raw.githubusercontent.com/freifunk/api.freifunk.net/master/specs/0.5.2.json"
      },
      {
        "fileMatch": [
          "/*.asmdef"
        ],
        "uri": "https://json.schemastore.org/asmdef.json"
      },
      {
        "fileMatch": [
          "/.babelrc",
          "/.babelrc.json",
          "/babel.config.json"
        ],
        "uri": "https://json.schemastore.org/babelrc.json"
      },
      {
        "fileMatch": [
          "/.backportrc.json"
        ],
        "uri": "https://json.schemastore.org/backportrc.json"
      },
      {
        "fileMatch": [
          "/database.beef.json"
        ],
        "uri": "https://raw.githubusercontent.com/Avanade/Beef/master/tools/Beef.CodeGen.Core/Schema/database.beef.json"
      },
      {
        "fileMatch": [
          "/entity.beef.json",
          "/refdata.beef.json",
          "/datamodel.beef.json"
        ],
        "uri": "https://raw.githubusercontent.com/Avanade/Beef/master/tools/Beef.CodeGen.Core/Schema/entity.beef.json"
      },
      {
        "fileMatch": [
          "/database.beef-5.json"
        ],
        "uri": "https://raw.githubusercontent.com/Avanade/Beef/master/tools/Beef.CodeGen.Core/Schema/database.beef-5.json"
      },
      {
        "fileMatch": [
          "/entity.beef-5.json",
          "/refdata.beef-5.json",
          "/datamodel.beef-5.json"
        ],
        "uri": "https://raw.githubusercontent.com/Avanade/Beef/master/tools/Beef.CodeGen.Core/Schema/entity.beef-5.json"
      },
      {
        "fileMatch": [
          "/*.bigquery.json"
        ],
        "uri": "https://json.schemastore.org/bigquery-table.json"
      },
      {
        "fileMatch": [
          "/bitrise.json"
        ],
        "uri": "https://json.schemastore.org/bitrise.json"
      },
      {
        "fileMatch": [
          "/.bootstraprc"
        ],
        "uri": "https://json.schemastore.org/bootstraprc.json"
      },
      {
        "fileMatch": [
          "/bower.json",
          "/.bower.json"
        ],
        "uri": "https://json.schemastore.org/bower.json"
      },
      {
        "fileMatch": [
          "/.bowerrc"
        ],
        "uri": "https://json.schemastore.org/bowerrc.json"
      },
      {
        "fileMatch": [
          "/.suite.json",
          "/.xsuite.json"
        ],
        "uri": "https://json.schemastore.org/bozr.json"
      },
      {
        "fileMatch": [
          "/bsconfig.json"
        ],
        "uri": "https://raw.githubusercontent.com/rescript-lang/rescript-compiler/master/docs/docson/build-schema.json"
      },
      {
        "fileMatch": [
          "/*buildinfo*.json",
          "/*build-info*.json",
          "/*.buildinfo"
        ],
        "uri": "https://raw.githubusercontent.com/jfrog/build-info-go/main/buildinfo-schema.json"
      },
      {
        "fileMatch": [
          "/buildkite.json",
          "/buildkite.*.json",
          "/**/.buildkite/pipeline.json",
          "/**/.buildkite/pipeline.*.json"
        ],
        "uri": "https://raw.githubusercontent.com/buildkite/pipeline-schema/main/schema.json"
      },
      {
        "fileMatch": [
          "/bundleconfig.json"
        ],
        "uri": "https://json.schemastore.org/bundleconfig.json"
      },
      {
        "fileMatch": [
          "/block.json"
        ],
        "uri": "https://schemas.wp.org/trunk/block.json"
      },
      {
        "fileMatch": [
          "/block-metadata.json"
        ],
        "uri": "https://blockprotocol.org/schemas/block-metadata.json"
      },
      {
        "fileMatch": [
          "/scripts.json",
          "/better-scripts.json",
          "/.better-scriptsrc",
          "/.better-scriptsrc.json"
        ],
        "uri": "https://raw.githubusercontent.com/iamyoki/better-scripts/main/lib/schema.json"
      },
      {
        "fileMatch": [
          "/CMakePresets.json",
          "/CMakeUserPresets.json"
        ],
        "uri": "https://raw.githubusercontent.com/Kitware/CMake/master/Help/manual/presets/schema.json"
      },
      {
        "uri": "https://carafe.fm/schema/draft-02/bundle.schema.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/cityjson/specs/master/schemas/cityjson.min.schema.json"
      },
      {
        "fileMatch": [
          "/*.cncc.json"
        ],
        "uri": "https://appliedengdesign.github.io/cnccodes-json-schema/draft/2022-07/schema"
      },
      {
        "uri": "https://raw.githubusercontent.com/Ortus-Solutions/vscode-commandbox/master/resources/schemas/box.schema.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/Ortus-Solutions/vscode-commandbox/master/resources/schemas/server.schema.json"
      },
      {
        "fileMatch": [
          "/Spacefile"
        ],
        "uri": "https://deta.space/assets/spacefile.schema.json"
      },
      {
        "fileMatch": [
          "/devbox.json"
        ],
        "uri": "https://raw.githubusercontent.com/jetpack-io/devbox/main/.schema/devbox.schema.json"
      },
      {
        "fileMatch": [
          "/Chart.lock"
        ],
        "uri": "https://json.schemastore.org/chart-lock.json"
      },
      {
        "fileMatch": [
          "/.codeclimate.json"
        ],
        "uri": "https://json.schemastore.org/codeclimate.json"
      },
      {
        "fileMatch": [
          "/.clasp.json"
        ],
        "uri": "https://json.schemastore.org/clasp.json"
      },
      {
        "fileMatch": [
          "/.clangd"
        ],
        "uri": "https://json.schemastore.org/clangd.json"
      },
      {
        "fileMatch": [
          "/clib.json"
        ],
        "uri": "https://json.schemastore.org/clib.json"
      },
      {
        "fileMatch": [
          "/codux.config.json"
        ],
        "uri": "https://wixplosives.github.io/codux-config-schema/codux.config.schema.json"
      },
      {
        "fileMatch": [
          "/devcontainer.json",
          "/.devcontainer.json"
        ],
        "uri": "https://raw.githubusercontent.com/devcontainers/spec/main/schemas/devContainer.schema.json"
      },
      {
        "fileMatch": [
          "/vcpkg.json"
        ],
        "uri": "https://raw.githubusercontent.com/microsoft/vcpkg-tool/main/docs/vcpkg.schema.json"
      },
      {
        "fileMatch": [
          "/vcpkg-configuration.json"
        ],
        "uri": "https://raw.githubusercontent.com/microsoft/vcpkg-tool/main/docs/vcpkg-configuration.schema.json"
      },
      {
        "fileMatch": [
          "/vercel.json"
        ],
        "uri": "https://openapi.vercel.sh/vercel.json"
      },
      {
        "fileMatch": [
          "/*.code-snippets"
        ],
        "uri": "https://raw.githubusercontent.com/Yash-Singh1/vscode-snippets-json-schema/main/schema.json"
      },
      {
        "fileMatch": [
          "/compilerconfig.json"
        ],
        "uri": "https://json.schemastore.org/compilerconfig.json"
      },
      {
        "fileMatch": [
          "/compile_commands.json"
        ],
        "uri": "https://json.schemastore.org/compile-commands.json"
      },
      {
        "fileMatch": [
          "/commands.json"
        ],
        "uri": "https://json.schemastore.org/commands.json"
      },
      {
        "fileMatch": [
          "/*.cat.json",
          "/*.catalog.json"
        ],
        "uri": "https://raw.githubusercontent.com/howlowck/common-catalog-schema/main/schema-versions.json"
      },
      {
        "fileMatch": [
          "/cosmos.config.json"
        ],
        "uri": "https://json.schemastore.org/cosmos-config.json"
      },
      {
        "uri": "https://json.schemastore.org/chrome-manifest.json"
      },
      {
        "fileMatch": [
          "/**/_locales/*/messages.json"
        ],
        "uri": "https://json.schemastore.org/chrome-extension-locales-messages.json"
      },
      {
        "fileMatch": [
          "/chutzpah.json"
        ],
        "uri": "https://json.schemastore.org/chutzpah.json"
      },
      {
        "fileMatch": [
          "/contentmanifest.json"
        ],
        "uri": "https://json.schemastore.org/vsix-manifestinjection.json"
      },
      {
        "fileMatch": [
          "/cloudbuild.json",
          "/*.cloudbuild.json"
        ],
        "uri": "https://json.schemastore.org/cloudbuild.json"
      },
      {
        "fileMatch": [
          "/workflows.json",
          "/*.workflows.json"
        ],
        "uri": "https://json.schemastore.org/workflows.json"
      },
      {
        "fileMatch": [
          "/cdk.json"
        ],
        "uri": "https://json.schemastore.org/cdk.json"
      },
      {
        "fileMatch": [
          "/*.cf.json",
          "/cloudformation.json"
        ],
        "uri": "https://raw.githubusercontent.com/awslabs/goformation/master/schema/cloudformation.schema.json"
      },
      {
        "fileMatch": [
          "/serverless.template",
          "/*.sam.json",
          "/sam.json"
        ],
        "uri": "https://raw.githubusercontent.com/aws/serverless-application-model/main/samtranslator/schema/schema.json"
      },
      {
        "fileMatch": [
          "/CITATION.cff"
        ],
        "uri": "https://citation-file-format.github.io/1.2.0/schema.json"
      },
      {
        "fileMatch": [
          "/coffeelint.json"
        ],
        "uri": "https://json.schemastore.org/coffeelint.json"
      },
      {
        "fileMatch": [
          "/composer.json"
        ],
        "uri": "https://getcomposer.org/schema.json"
      },
      {
        "fileMatch": [
          "/component.json"
        ],
        "uri": "https://json.schemastore.org/component.json"
      },
      {
        "fileMatch": [
          "/cdmanifest.json",
          "/cgmanifest.json"
        ],
        "uri": "https://json.schemastore.org/component-detection-manifest.json"
      },
      {
        "fileMatch": [
          "/contribute.json"
        ],
        "uri": "https://raw.githubusercontent.com/mozilla/contribute.json/master/schema.json"
      },
      {
        "fileMatch": [
          "/cypress.json"
        ],
        "uri": "https://on.cypress.io/cypress.schema.json"
      },
      {
        "fileMatch": [
          "/.creatomic"
        ],
        "uri": "https://json.schemastore.org/creatomic.json"
      },
      {
        "fileMatch": [
          "/.cspell.json",
          "/cspell.json",
          "/.cSpell.json",
          "/cSpell.json",
          "/cspell.config.json"
        ],
        "uri": "https://raw.githubusercontent.com/streetsidesoftware/cspell/main/packages/cspell-types/cspell.schema.json"
      },
      {
        "fileMatch": [
          "/.csscomb.json"
        ],
        "uri": "https://json.schemastore.org/csscomb.json"
      },
      {
        "fileMatch": [
          "/.csslintrc"
        ],
        "uri": "https://json.schemastore.org/csslintrc.json"
      },
      {
        "uri": "https://json.schemastore.org/dart-build.json"
      },
      {
        "fileMatch": [
          "/.dla.json"
        ],
        "uri": "https://json.schemastore.org/datalogic-scan2deploy-android.json"
      },
      {
        "fileMatch": [
          "/.dlc.json"
        ],
        "uri": "https://json.schemastore.org/datalogic-scan2deploy-ce.json"
      },
      {
        "fileMatch": [
          "/debugsettings.json"
        ],
        "uri": "https://json.schemastore.org/debugsettings.json"
      },
      {
        "fileMatch": [
          "/deno.json",
          "/deno.jsonc"
        ],
        "uri": "https://raw.githubusercontent.com/denoland/deno/main/cli/schemas/config-file.v1.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/AxoCode/json-schema/master/discord/webhook.json"
      },
      {
        "fileMatch": [
          "/dockerd.json",
          "/docker.json"
        ],
        "uri": "https://json.schemastore.org/dockerd.json"
      },
      {
        "fileMatch": [
          "/docker-seq.json",
          "/*.docker-seq.json"
        ],
        "uri": "https://gitlab.com/sbenv/veroxis/docker-seq/-/raw/HEAD/docker-seq.schema.json"
      },
      {
        "fileMatch": [
          "/docfx.json"
        ],
        "uri": "https://json.schemastore.org/docfx.json"
      },
      {
        "fileMatch": [
          "/**/.dolittle/artifacts.json"
        ],
        "uri": "https://raw.githubusercontent.com/dolittle/DotNET.SDK/v5.0.0/Schemas/Artifacts.Configuration/artifacts.json"
      },
      {
        "fileMatch": [
          "/bounded-context.json"
        ],
        "uri": "https://raw.githubusercontent.com/dolittle/Runtime/master/Schemas/Applications.Configuration/bounded-context.json"
      },
      {
        "fileMatch": [
          "/**/.dolittle/event-horizons.json"
        ],
        "uri": "https://raw.githubusercontent.com/dolittle/Runtime/master/Schemas/Events/event-horizons.json"
      },
      {
        "fileMatch": [
          "/**/.dolittle/resources.json"
        ],
        "uri": "https://raw.githubusercontent.com/dolittle/DotNET.Fundamentals/v5.1.0/Schemas/ResourceTypes.Configuration/resources.json"
      },
      {
        "fileMatch": [
          "/**/.dolittle/server.json"
        ],
        "uri": "https://raw.githubusercontent.com/dolittle/Runtime/master/Schemas/Server/server.json"
      },
      {
        "fileMatch": [
          "/**/.dolittle/tenants.json"
        ],
        "uri": "https://raw.githubusercontent.com/dolittle/Runtime/master/Schemas/Tenancy/tenants.json"
      },
      {
        "fileMatch": [
          "/**/.dolittle/tenant-map.json"
        ],
        "uri": "https://raw.githubusercontent.com/dolittle/DotNET.Fundamentals/master/Schemas/Tenancy.Configuration/tenant-map.json"
      },
      {
        "fileMatch": [
          "/**/.dolittle/topology.json"
        ],
        "uri": "https://raw.githubusercontent.com/dolittle/DotNET.SDK/master/Schemas/Applications.Configuration/topology.json"
      },
      {
        "fileMatch": [
          "/dotnet-release-index.json"
        ],
        "uri": "https://json.schemastore.org/dotnet-releases-index.json"
      },
      {
        "fileMatch": [
          "/dotnetcli.host.json"
        ],
        "uri": "https://json.schemastore.org/dotnetcli.host.json"
      },
      {
        "fileMatch": [
          "/dprint.json",
          "/dprint.jsonc",
          "/.dprint.json",
          "/.dprint.jsonc"
        ],
        "uri": "https://dprint.dev/schemas/v0.json"
      },
      {
        "uri": "https://json.schemastore.org/dss-2.0.0.json"
      },
      {
        "fileMatch": [
          "/ecosystem.json",
          "/ecosystem.config.json"
        ],
        "uri": "https://json.schemastore.org/pm2-ecosystem.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/weaveworks/eksctl/main/pkg/apis/eksctl.io/v1alpha5/assets/schema.json"
      },
      {
        "fileMatch": [
          "/.esmrc",
          "/.esmrc.json",
          "/.esmrc.js",
          "/.esmrc.cjs",
          "/.esmrc.mjs"
        ],
        "uri": "https://json.schemastore.org/esmrc.json"
      },
      {
        "uri": "https://json.schemastore.org/esquio.json"
      },
      {
        "fileMatch": [
          "/epr-manifest.json"
        ],
        "uri": "https://json.schemastore.org/epr-manifest.json"
      },
      {
        "fileMatch": [
          "/electron-builder.json"
        ],
        "uri": "https://json.schemastore.org/electron-builder.json"
      },
      {
        "fileMatch": [
          "/app.json"
        ],
        "uri": "https://json.schemastore.org/expo-46.0.0.json"
      },
      {
        "fileMatch": [
          "/ezd.json"
        ],
        "uri": "https://gitlab.com/sbenv/veroxis/ezd-rs/-/raw/HEAD/ezd.schema.json"
      },
      {
        "fileMatch": [
          "/.eslintrc",
          "/.eslintrc.json"
        ],
        "uri": "https://json.schemastore.org/eslintrc.json"
      },
      {
        "fileMatch": [
          "/**/application/instances/*.json"
        ],
        "uri": "https://www.facets.cloud/assets/fsdl/application.schema.json"
      },
      {
        "fileMatch": [
          "/fabric.mod.json"
        ],
        "uri": "https://json.schemastore.org/fabric.mod.json"
      },
      {
        "fileMatch": [
          "/firebase.json"
        ],
        "uri": "https://raw.githubusercontent.com/firebase/firebase-tools/master/schema/firebase-config.json"
      },
      {
        "fileMatch": [
          "/**/.well-known/first-party-set.json"
        ],
        "uri": "https://raw.githubusercontent.com/GoogleChrome/related-website-sets/main/SCHEMA.json"
      },
      {
        "fileMatch": [
          "/*_fiqus.json",
          "/*_fiqus.json5",
          "/*_FiQuS.json",
          "/*_FiQuS.json5"
        ],
        "uri": "https://gitlab.cern.ch/steam/fiqus/-/raw/master/docs/schema.json"
      },
      {
        "uri": "https://json.schemastore.org/foundryvtt-base-package-manifest.json"
      },
      {
        "fileMatch": [
          "/**/modules/*/module.json"
        ],
        "uri": "https://json.schemastore.org/foundryvtt-module-manifest.json"
      },
      {
        "fileMatch": [
          "/**/systems/*/system.json"
        ],
        "uri": "https://json.schemastore.org/foundryvtt-system-manifest.json"
      },
      {
        "fileMatch": [
          "/**/worlds/*/world.json"
        ],
        "uri": "https://json.schemastore.org/foundryvtt-world-manifest.json"
      },
      {
        "fileMatch": [
          "/**/systems/*/template.json"
        ],
        "uri": "https://json.schemastore.org/foundryvtt-template.json"
      },
      {
        "fileMatch": [
          "/fossa-deps.json"
        ],
        "uri": "https://raw.githubusercontent.com/fossas/fossa-cli/master/docs/references/files/fossa-deps.schema.json"
      },
      {
        "fileMatch": [
          "/karakum.config.json"
        ],
        "uri": "https://raw.githubusercontent.com/karakum-team/karakum/master/schema/karakum-schema.json"
      },
      {
        "fileMatch": [
          "/function.json"
        ],
        "uri": "https://json.schemastore.org/function.json"
      },
      {
        "fileMatch": [
          "/config-g2p.json"
        ],
        "uri": "https://raw.githubusercontent.com/roedoejet/g2p/main/g2p/mappings/.schema/g2p-config-schema-2.0.json"
      },
      {
        "fileMatch": [
          "/gaspar.config.json"
        ],
        "uri": "https://json.schemastore.org/gaspar-1.0.json"
      },
      {
        "fileMatch": [
          "/gwcore.json",
          "/gatewaycore.json",
          "/*.gwcore.json",
          "/*.gatewaycore.json"
        ],
        "uri": "https://raw.githubusercontent.com/cloudtoid/gateway-core/master/src/Cloudtoid.GatewayCore/Options/Schema/2021-07.json"
      },
      {
        "fileMatch": [
          "/**/.well-known/gpc.json"
        ],
        "uri": "https://json.schemastore.org/gpc.json"
      },
      {
        "uri": "https://json.schemastore.org/geojson.json"
      },
      {
        "fileMatch": [
          "/**/.github/workflow-templates/**.properties.json"
        ],
        "uri": "https://json.schemastore.org/github-workflow-template-properties.json"
      },
      {
        "fileMatch": [
          "/global.json"
        ],
        "uri": "https://json.schemastore.org/global.json"
      },
      {
        "fileMatch": [
          "/.golangci.json"
        ],
        "uri": "https://json.schemastore.org/golangci-lint.json"
      },
      {
        "fileMatch": [
          "/*.goff.json"
        ],
        "uri": "https://raw.githubusercontent.com/thomaspoignant/go-feature-flag/main/.schema/flag-schema.json"
      },
      {
        "uri": "https://goreleaser.com/static/schema-pro.json"
      },
      {
        "fileMatch": [
          "/goss.json"
        ],
        "uri": "https://github.com/goss-org/goss/raw/master/docs/goss-json-schema.yaml"
      },
      {
        "uri": "https://json.schemastore.org/grafana-dashboard-5.x.json"
      },
      {
        "fileMatch": [
          "/.meshrc.json",
          "/.meshrc.js"
        ],
        "uri": "https://unpkg.com/@graphql-mesh/types/esm/config-schema.json"
      },
      {
        "fileMatch": [
          "/graphql.config.json",
          "/graphql.config.js",
          "/.graphqlrc",
          "/.graphqlrc.json",
          "/.graphqlrc.js"
        ],
        "uri": "https://unpkg.com/graphql-config/config-schema.json"
      },
      {
        "fileMatch": [
          "/codegen.json",
          "/codegen.js",
          "/.codegen.json",
          "/.codegen.js"
        ],
        "uri": "https://www.graphql-code-generator.com/config.schema.json"
      },
      {
        "fileMatch": [
          "/copy.json"
        ],
        "uri": "https://json.schemastore.org/grunt-copy-task.json"
      },
      {
        "fileMatch": [
          "/clean.json"
        ],
        "uri": "https://json.schemastore.org/grunt-clean-task.json"
      },
      {
        "fileMatch": [
          "/cssmin.json"
        ],
        "uri": "https://json.schemastore.org/grunt-cssmin-task.json"
      },
      {
        "fileMatch": [
          "/jshint.json"
        ],
        "uri": "https://json.schemastore.org/grunt-jshint-task.json"
      },
      {
        "fileMatch": [
          "/watch.json"
        ],
        "uri": "https://json.schemastore.org/grunt-watch-task.json"
      },
      {
        "fileMatch": [
          "/**/grunt/*.json",
          "/*-tasks.json"
        ],
        "uri": "https://json.schemastore.org/grunt-task.json"
      },
      {
        "fileMatch": [
          "/haxelib.json"
        ],
        "uri": "https://raw.githubusercontent.com/HaxeFoundation/haxelib/master/schema.json"
      },
      {
        "fileMatch": [
          "/*.hayson.json"
        ],
        "uri": "https://raw.githubusercontent.com/j2inn/hayson/master/hayson-json-schema.json"
      },
      {
        "fileMatch": [
          "/hazelcast*.json",
          "/hz-*.json"
        ],
        "uri": "https://hazelcast.com/schema/config/hazelcast-config-5.3.json"
      },
      {
        "fileMatch": [
          "/host.json"
        ],
        "uri": "https://json.schemastore.org/host.json"
      },
      {
        "fileMatch": [
          "/host-meta.json"
        ],
        "uri": "https://json.schemastore.org/host-meta.json"
      },
      {
        "fileMatch": [
          "/.htmlhintrc"
        ],
        "uri": "https://json.schemastore.org/htmlhint.json"
      },
      {
        "fileMatch": [
          "/hydra.json"
        ],
        "uri": "https://raw.githubusercontent.com/ory/hydra/master/.schema/version.schema.json"
      },
      {
        "fileMatch": [
          "/zapp.json"
        ],
        "uri": "https://raw.githubusercontent.com/IBM/zopeneditor-about/main/zapp/zapp-schema-1.0.0.json"
      },
      {
        "fileMatch": [
          "/zcodeformat.json"
        ],
        "uri": "https://raw.githubusercontent.com/IBM/zopeneditor-about/main/zcodeformat/zcodeformat-schema-0.0.1.json"
      },
      {
        "fileMatch": [
          "/ide.host.json"
        ],
        "uri": "https://json.schemastore.org/ide.host.json"
      },
      {
        "fileMatch": [
          "/imageoptimizer.json"
        ],
        "uri": "https://json.schemastore.org/imageoptimizer.json"
      },
      {
        "fileMatch": [
          "/.imgbotconfig"
        ],
        "uri": "https://json.schemastore.org/imgbotconfig.json"
      },
      {
        "fileMatch": [
          "/importmap.json",
          "/import_map.json",
          "/import-map.json"
        ],
        "uri": "https://json.schemastore.org/importmap.json"
      },
      {
        "fileMatch": [
          "/iobroker.json",
          "/iobroker-dist.json"
        ],
        "uri": "https://raw.githubusercontent.com/ioBroker/ioBroker.js-controller/master/schemas/iobroker.json"
      },
      {
        "fileMatch": [
          "/jsonConfig.json",
          "/jsonCustom.json",
          "/jsonTab.json"
        ],
        "uri": "https://raw.githubusercontent.com/ioBroker/adapter-react-v5/main/schemas/jsonConfig.json"
      },
      {
        "fileMatch": [
          "/io-package.json"
        ],
        "uri": "https://raw.githubusercontent.com/ioBroker/ioBroker.js-controller/master/schemas/io-package.json"
      },
      {
        "fileMatch": [
          "/jasmine.json"
        ],
        "uri": "https://json.schemastore.org/jasmine.json"
      },
      {
        "fileMatch": [
          "/*.jd2cr",
          "/*.jd2cr.json"
        ],
        "uri": "https://raw.githubusercontent.com/sergxerj/jdownloader2-crawler-rule-json-schema/main/jd2cr.schema.json"
      },
      {
        "fileMatch": [
          "/*.jd2mcr",
          "/*.jd2mcr.json",
          "/*.linkcrawlerrules.json"
        ],
        "uri": "https://raw.githubusercontent.com/sergxerj/jdownloader2-crawler-rule-json-schema/main/jd2mcr.schema.json"
      },
      {
        "fileMatch": [
          "/**/filespecs/*.json",
          "/*filespec*.json",
          "/*.filespec"
        ],
        "uri": "https://raw.githubusercontent.com/jfrog/jfrog-cli/v2/schema/filespec-schema.json"
      },
      {
        "fileMatch": [
          "/*.jmdsl.json"
        ],
        "uri": "https://github.com/abstracta/jmeter-java-dsl/releases/latest/download/jmdsl-config-schema.json"
      },
      {
        "uri": "https://json.schemastore.org/jovo-language-model.json"
      },
      {
        "fileMatch": [
          "/jreleaser.json"
        ],
        "uri": "https://json.schemastore.org/jreleaser-1.9.0.json"
      },
      {
        "fileMatch": [
          "/.jsbeautifyrc"
        ],
        "uri": "https://json.schemastore.org/jsbeautifyrc.json"
      },
      {
        "fileMatch": [
          "/.jsbeautifyrc"
        ],
        "uri": "https://json.schemastore.org/jsbeautifyrc-nested.json"
      },
      {
        "fileMatch": [
          "/.jscsrc",
          "/jscsrc.json"
        ],
        "uri": "https://json.schemastore.org/jscsrc.json"
      },
      {
        "fileMatch": [
          "/.jshintrc"
        ],
        "uri": "https://json.schemastore.org/jshintrc.json"
      },
      {
        "fileMatch": [
          "/.jsinspectrc"
        ],
        "uri": "https://json.schemastore.org/jsinspectrc.json"
      },
      {
        "uri": "https://jsonapi.org/schema"
      },
      {
        "uri": "https://json.schemastore.org/jdt.json"
      },
      {
        "fileMatch": [
          "/feed.json"
        ],
        "uri": "https://json.schemastore.org/feed.json"
      },
      {
        "fileMatch": [
          "/*.jsonld"
        ],
        "uri": "https://json.schemastore.org/jsonld.json"
      },
      {
        "fileMatch": [
          "/*.patch",
          "/*.patch.json"
        ],
        "uri": "https://json.schemastore.org/json-patch.json"
      },
      {
        "fileMatch": [
          "/jsconfig.json"
        ],
        "uri": "https://json.schemastore.org/jsconfig.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/siemens/kas/master/kas/schema-kas.json"
      },
      {
        "fileMatch": [
          "/krakend.json"
        ],
        "uri": "https://www.krakend.io/schema/krakend.json"
      },
      {
        "fileMatch": [
          "/service.datadog.json"
        ],
        "uri": "https://raw.githubusercontent.com/DataDog/schema/main/service-catalog/version.schema.json"
      },
      {
        "fileMatch": [
          "/keto.json"
        ],
        "uri": "https://raw.githubusercontent.com/ory/keto/master/.schema/version.schema.json"
      },
      {
        "fileMatch": [
          "/launchsettings.json"
        ],
        "uri": "https://json.schemastore.org/launchsettings.json"
      },
      {
        "fileMatch": [
          "/{.lefthook,lefthook,lefthook-local,.lefthook-local}.{yml,yaml,toml,json}"
        ],
        "uri": "https://json.schemastore.org/lefthook.json"
      },
      {
        "fileMatch": [
          "/lego.json"
        ],
        "uri": "https://json.schemastore.org/lego.json"
      },
      {
        "fileMatch": [
          "/lerna.json"
        ],
        "uri": "https://json.schemastore.org/lerna.json"
      },
      {
        "fileMatch": [
          "/libman.json"
        ],
        "uri": "https://json.schemastore.org/libman.json"
      },
      {
        "fileMatch": [
          "/license-report-config.json"
        ],
        "uri": "https://json.schemastore.org/license-report-config.json"
      },
      {
        "fileMatch": [
          "/linkinator.config.json"
        ],
        "uri": "https://json.schemastore.org/linkinator-config.json"
      },
      {
        "uri": "https://w3id.org/linkml/meta.schema.json"
      },
      {
        "fileMatch": [
          "/LivelyProperties.json"
        ],
        "uri": "https://raw.githubusercontent.com/rocksdanister/lively/core-separation/schemas/livelyPropertiesSchema.json"
      },
      {
        "fileMatch": [
          "/local.settings.json"
        ],
        "uri": "https://json.schemastore.org/local.settings.json"
      },
      {
        "fileMatch": [
          "/localazy.json"
        ],
        "uri": "https://raw.githubusercontent.com/localazy/cli-schema/master/localazy.json"
      },
      {
        "fileMatch": [
          "/*.lsdl.json"
        ],
        "uri": "https://json.schemastore.org/lsdlschema.json"
      },
      {
        "fileMatch": [
          "/*.settings.json"
        ],
        "uri": "https://json.schemastore.org/micro.json"
      },
      {
        "fileMatch": [
          "/meltano-manifest.json",
          "/meltano-manifest.*.json"
        ],
        "uri": "https://raw.githubusercontent.com/meltano/meltano/main/src/meltano/schemas/meltano.schema.json"
      },
      {
        "uri": "https://json.schemastore.org/band-manifest.json"
      },
      {
        "fileMatch": [
          "/mimetypes.json"
        ],
        "uri": "https://json.schemastore.org/mimetypes.json"
      },
      {
        "fileMatch": [
          "/**/data/*/advancements/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-advancement.json"
      },
      {
        "fileMatch": [
          "/**/data/*/worldgen/biome/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-biome.json"
      },
      {
        "fileMatch": [
          "/**/data/*/worldgen/configured_carver/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-configured-carver.json"
      },
      {
        "fileMatch": [
          "/**/data/*/damage_type/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-damage-type.json"
      },
      {
        "fileMatch": [
          "/**/data/*/dimension_type/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-dimension-type.json"
      },
      {
        "fileMatch": [
          "/**/data/*/dimension/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-dimension.json"
      },
      {
        "fileMatch": [
          "/**/data/*/item_modifiers/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-item-modifier.json"
      },
      {
        "fileMatch": [
          "/**/data/*/loot_tables/**/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-loot-table.json"
      },
      {
        "fileMatch": [
          "/**/pack.mcmeta"
        ],
        "uri": "https://json.schemastore.org/minecraft-pack-mcmeta.json"
      },
      {
        "fileMatch": [
          "/**/data/*/predicates/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-predicate.json"
      },
      {
        "fileMatch": [
          "/**/data/*/recipes/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-recipe.json"
      },
      {
        "fileMatch": [
          "/**/data/*/tags/**/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-tag.json"
      },
      {
        "fileMatch": [
          "/**/data/*/worldgen/template_pool/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-template-pool.json"
      },
      {
        "fileMatch": [
          "/**/assets/*/lang/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-lang.json"
      },
      {
        "fileMatch": [
          "/**/assets/*/particles/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-particle.json"
      },
      {
        "fileMatch": [
          "/**/assets/*/sounds.json"
        ],
        "uri": "https://raw.githubusercontent.com/AxoCode/json-schema/master/minecraft/sounds.json"
      },
      {
        "fileMatch": [
          "/**/assets/*/textures/**/*.png.mcmeta"
        ],
        "uri": "https://json.schemastore.org/minecraft-texture-mcmeta.json"
      },
      {
        "fileMatch": [
          "/**/data/*/trim_material/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-trim-material.json"
      },
      {
        "fileMatch": [
          "/**/data/*/trim_pattern/*.json"
        ],
        "uri": "https://json.schemastore.org/minecraft-trim-pattern.json"
      },
      {
        "fileMatch": [
          "/ms2rescore.json",
          "/.*-ms2rescore.json",
          "/.*-ms2rescore-config.json"
        ],
        "uri": "https://raw.githubusercontent.com/compomics/ms2rescore/main/ms2rescore/package_data/config_schema.json"
      },
      {
        "fileMatch": [
          "/.mocharc.json",
          "/.mocharc.jsonc"
        ],
        "uri": "https://json.schemastore.org/mocharc.json"
      },
      {
        "fileMatch": [
          "/.modernizrrc"
        ],
        "uri": "https://json.schemastore.org/modernizrrc.json"
      },
      {
        "fileMatch": [
          "/mycode.json"
        ],
        "uri": "https://json.schemastore.org/mycode.json"
      },
      {
        "fileMatch": [
          "/nightwatch.json"
        ],
        "uri": "https://json.schemastore.org/nightwatch.json"
      },
      {
        "uri": "https://json.schemastore.org/ninjs-2.0.json"
      },
      {
        "uri": "https://json.schemastore.org/ninjs-1.3.json"
      },
      {
        "fileMatch": [
          "/.nestcli.json",
          "/.nest-cli.json",
          "/nest-cli.json",
          "/nest.json"
        ],
        "uri": "https://json.schemastore.org/nest-cli.json"
      },
      {
        "fileMatch": [
          "/nlu.json",
          "/.nlu.json"
        ],
        "uri": "https://raw.githubusercontent.com/oresoftware/npm-link-up/master/assets/nlu.schema.json"
      },
      {
        "fileMatch": [
          "/.nodehawkrc"
        ],
        "uri": "https://json.schemastore.org/nodehawkrc.json"
      },
      {
        "fileMatch": [
          "/nodemon.json"
        ],
        "uri": "https://json.schemastore.org/nodemon.json"
      },
      {
        "fileMatch": [
          "/service.nox.json"
        ],
        "uri": "https://noxorg.dev/schemas/NoxConfiguration.json"
      },
      {
        "fileMatch": [
          "/.npmpackagejsonlintrc",
          "/npmpackagejsonlintrc.json",
          "/.npmpackagejsonlintrc.json"
        ],
        "uri": "https://json.schemastore.org/npmpackagejsonlintrc.json"
      },
      {
        "uri": "https://json.schemastore.org/npm-badges.json"
      },
      {
        "uri": "https://json.schemastore.org/nuget-project.json"
      },
      {
        "fileMatch": [
          "/nswag.json"
        ],
        "uri": "https://json.schemastore.org/nswag.json"
      },
      {
        "fileMatch": [
          "/ntangle.json",
          "/ntangle.jsn"
        ],
        "uri": "https://raw.githubusercontent.com/Avanade/NTangle/main/schemas/ntangle.json"
      },
      {
        "fileMatch": [
          "/oathkeeper.json"
        ],
        "uri": "https://raw.githubusercontent.com/ory/oathkeeper/master/.schema/version.schema.json"
      },
      {
        "fileMatch": [
          "/ocelot.json"
        ],
        "uri": "https://json.schemastore.org/ocelot.json"
      },
      {
        "fileMatch": [
          "/omnisharp.json"
        ],
        "uri": "https://json.schemastore.org/omnisharp.json"
      },
      {
        "fileMatch": [
          "/openapi.json"
        ],
        "uri": "https://raw.githubusercontent.com/OAI/OpenAPI-Specification/main/schemas/v3.1/schema.json"
      },
      {
        "fileMatch": [
          "/openrpc.json",
          "/open-rpc.json"
        ],
        "uri": "https://meta.open-rpc.org/"
      },
      {
        "fileMatch": [
          "/*.ustx"
        ],
        "uri": "https://json.schemastore.org/openutau-ustx.json"
      },
      {
        "uri": "https://json.schemastore.org/openfin.json"
      },
      {
        "fileMatch": [
          "/kratos.json"
        ],
        "uri": "https://raw.githubusercontent.com/ory/kratos/master/.schema/version.schema.json"
      },
      {
        "fileMatch": [
          "/package.json"
        ],
        "uri": "https://json.schemastore.org/package.json"
      },
      {
        "fileMatch": [
          "/package.manifest"
        ],
        "uri": "https://json.schemastore.org/package.manifest.json"
      },
      {
        "fileMatch": [
          "/packer.json"
        ],
        "uri": "https://json.schemastore.org/packer.json"
      },
      {
        "fileMatch": [
          "/submol*.json"
        ],
        "uri": "https://json.schemastore.org/pgap_yaml_input_reader.json"
      },
      {
        "fileMatch": [
          "/pattern.json"
        ],
        "uri": "https://json.schemastore.org/pattern.json"
      },
      {
        "uri": "https://json.schemastore.org/poetry.json"
      },
      {
        "fileMatch": [
          "/plagiarize.json"
        ],
        "uri": "https://json.schemastore.org/plagiarize.json"
      },
      {
        "fileMatch": [
          "/plagiarize-me.json"
        ],
        "uri": "https://json.schemastore.org/plagiarize-me.json"
      },
      {
        "fileMatch": [
          "/portman-config.json",
          "/portman.json"
        ],
        "uri": "https://raw.githubusercontent.com/apideck-libraries/portman/main/src/utils/portman-config-schema.json"
      },
      {
        "fileMatch": [
          "/.postcssrc",
          "/.postcssrc.json"
        ],
        "uri": "https://json.schemastore.org/postcssrc.json"
      },
      {
        "fileMatch": [
          "/*.postman_collection.json"
        ],
        "uri": "https://schema.postman.com/collection/json/v2.1.0/draft-07/collection.json"
      },
      {
        "fileMatch": [
          "/.powerpages-web-template-manifest"
        ],
        "uri": "https://json.schemastore.org/powerpages-web-template-manifest.json"
      },
      {
        "fileMatch": [
          "/.prettierrc",
          "/.prettierrc.json"
        ],
        "uri": "https://json.schemastore.org/prettierrc.json"
      },
      {
        "fileMatch": [
          "/project.json"
        ],
        "uri": "https://json.schemastore.org/project.json"
      },
      {
        "uri": "https://json.schemastore.org/project-1.0.0-beta3.json"
      },
      {
        "uri": "https://json.schemastore.org/project-1.0.0-beta4.json"
      },
      {
        "uri": "https://json.schemastore.org/project-1.0.0-beta5.json"
      },
      {
        "uri": "https://json.schemastore.org/project-1.0.0-beta6.json"
      },
      {
        "uri": "https://json.schemastore.org/project-1.0.0-beta8.json"
      },
      {
        "uri": "https://json.schemastore.org/project-1.0.0-rc1.json"
      },
      {
        "uri": "https://json.schemastore.org/project-1.0.0-rc2.json"
      },
      {
        "fileMatch": [
          "/proxies.json"
        ],
        "uri": "https://json.schemastore.org/proxies.json"
      },
      {
        "fileMatch": [
          "/.putout.json"
        ],
        "uri": "https://json.schemastore.org/putout.json"
      },
      {
        "fileMatch": [
          "/pyrseas-0.8.json"
        ],
        "uri": "https://json.schemastore.org/pyrseas-0.8.json"
      },
      {
        "fileMatch": [
          "/pyrightconfig.json"
        ],
        "uri": "https://raw.githubusercontent.com/microsoft/pyright/main/packages/vscode-pyright/schemas/pyrightconfig.schema.json"
      },
      {
        "fileMatch": [
          "/_qgoda.json",
          "/_localqgoda.json"
        ],
        "uri": "https://www.qgoda.net/schemas/qgoda.json"
      },
      {
        "fileMatch": [
          "/info.json"
        ],
        "uri": "https://raw.githubusercontent.com/Cog-Creators/Red-DiscordBot/V3/develop/schema/red_cog.schema.json"
      },
      {
        "fileMatch": [
          "/info.json"
        ],
        "uri": "https://raw.githubusercontent.com/Cog-Creators/Red-DiscordBot/V3/develop/schema/red_cog_repo.schema.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/Cog-Creators/Red-DiscordBot/V3/develop/schema/trivia.schema.json"
      },
      {
        "fileMatch": [
          "/.rehyperc",
          "/.rehyperc.json"
        ],
        "uri": "https://json.schemastore.org/rehyperc.json"
      },
      {
        "fileMatch": [
          "/.remarkrc",
          "/.remarkrc.json"
        ],
        "uri": "https://json.schemastore.org/remarkrc.json"
      },
      {
        "fileMatch": [
          "/*.resjson"
        ],
        "uri": "https://json.schemastore.org/resjson.json"
      },
      {
        "fileMatch": [
          "/rust-project.json"
        ],
        "uri": "https://json.schemastore.org/rust-project.json"
      },
      {
        "fileMatch": [
          "/**/resume.json",
          "/**/*.resume.json"
        ],
        "uri": "https://raw.githubusercontent.com/jsonresume/resume-schema/v1.0.0/schema.json"
      },
      {
        "fileMatch": [
          "/renovate.json",
          "/renovate.json5",
          "/**/.github/renovate.json",
          "/**/.github/renovate.json5",
          "/**/.gitlab/renovate.json",
          "/**/.gitlab/renovate.json5",
          "/.renovaterc",
          "/.renovaterc.json"
        ],
        "uri": "https://docs.renovatebot.com/renovate-schema.json"
      },
      {
        "fileMatch": [
          "/*_CV.json",
          "/*_CV.json5",
          "/*_cv.json",
          "/*_cv.json5"
        ],
        "uri": "https://raw.githubusercontent.com/sinaatalay/rendercv/main/schema.json"
      },
      {
        "fileMatch": [
          "/.sapphirerc.json"
        ],
        "uri": "https://raw.githubusercontent.com/sapphiredev/cli/main/templates/schemas/.sapphirerc.scheme.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-1.0.0.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-2.0.0.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-2.1.0-rtm.2.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-external-property-file-2.1.0-rtm.2.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-2.1.0-rtm.3.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-external-property-file-2.1.0-rtm.3.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-2.1.0-rtm.4.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-external-property-file-2.1.0-rtm.4.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-2.1.0-rtm.5.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-2.1.0-rtm.6.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-external-property-file-2.1.0-rtm.5.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-2.1.0.json"
      },
      {
        "uri": "https://json.schemastore.org/sarif-external-property-file-2.1.0.json"
      },
      {
        "uri": "https://json.schemastore.org/schema-catalog.json"
      },
      {
        "uri": "https://json.schemastore.org/schema-org-action.json"
      },
      {
        "uri": "https://json.schemastore.org/schema-org-contact-point.json"
      },
      {
        "uri": "https://json.schemastore.org/schema-org-place.json"
      },
      {
        "uri": "https://json.schemastore.org/schema-org-thing.json"
      },
      {
        "fileMatch": [
          "/**/bucket/**.json"
        ],
        "uri": "https://raw.githubusercontent.com/lukesampson/scoop/master/schema.json"
      },
      {
        "fileMatch": [
          "/.releaserc",
          "/.releaserc.json"
        ],
        "uri": "https://json.schemastore.org/semantic-release.json"
      },
      {
        "fileMatch": [
          "/sergen.json",
          "/sergen.*.json",
          "/*.sergen.json"
        ],
        "uri": "https://json.schemastore.org/sergen.json"
      },
      {
        "fileMatch": [
          "/settings.job"
        ],
        "uri": "https://json.schemastore.org/settings.job.json"
      },
      {
        "fileMatch": [
          "/settings.paf",
          "/Settings.paf"
        ],
        "uri": "https://raw.githubusercontent.com/qualisys/qualisys-schemas/master/paf-module.schema.json"
      },
      {
        "uri": "https://json.schemastore.org/setuptools.json"
      },
      {
        "fileMatch": [
          "/silkit.json",
          "/*.silkit.json"
        ],
        "uri": "https://json.schemastore.org/sil-kit-participant-configuration.json"
      },
      {
        "fileMatch": [
          "/silkit-registry.json",
          "/*.silkit-registry.json"
        ],
        "uri": "https://json.schemastore.org/sil-kit-registry-configuration.json"
      },
      {
        "fileMatch": [
          "/.size-limit.json"
        ],
        "uri": "https://json.schemastore.org/size-limit.json"
      },
      {
        "uri": "https://json.schemastore.org/slack-app-manifest.json"
      },
      {
        "fileMatch": [
          "/skyuxconfig.json",
          "/skyuxconfig.*.json"
        ],
        "uri": "https://raw.githubusercontent.com/blackbaud/skyux-config/master/skyuxconfig-schema.json"
      },
      {
        "fileMatch": [
          "/.solidarity",
          "/.solidarity.json"
        ],
        "uri": "https://json.schemastore.org/solidaritySchema.json"
      },
      {
        "fileMatch": [
          "/*.slnf"
        ],
        "uri": "https://json.schemastore.org/solution-filter.json"
      },
      {
        "fileMatch": [
          "/*.map"
        ],
        "uri": "https://json.schemastore.org/sourcemap-v3.json"
      },
      {
        "fileMatch": [
          "/*.specif",
          "/*.specif.json"
        ],
        "uri": "https://json.schemastore.org/specif-1.1.json"
      },
      {
        "fileMatch": [
          "/*.mixins.json"
        ],
        "uri": "https://json.schemastore.org/sponge-mixins.json"
      },
      {
        "fileMatch": [
          "/*.sprite"
        ],
        "uri": "https://json.schemastore.org/sprite.json"
      },
      {
        "fileMatch": [
          "/sqlc.json"
        ],
        "uri": "https://json.schemastore.org/sqlc-2.0.json"
      },
      {
        "fileMatch": [
          "/staticwebapp.config.json"
        ],
        "uri": "https://json.schemastore.org/staticwebapp.config.json"
      },
      {
        "fileMatch": [
          "/swa-cli.config.json"
        ],
        "uri": "https://json.schemastore.org/swa-cli.config.json"
      },
      {
        "fileMatch": [
          "/.stackblitzrc",
          "/**/.stackblitz/config.json"
        ],
        "uri": "https://json.schemastore.org/stackblitzrc.json"
      },
      {
        "fileMatch": [
          "/stripe-app.json"
        ],
        "uri": "https://raw.githubusercontent.com/stripe/stripe-apps/main/schema/stripe-app.schema.json"
      },
      {
        "fileMatch": [
          "/stripe-app.*.json"
        ],
        "uri": "https://raw.githubusercontent.com/stripe/stripe-apps/main/schema/stripe-app-local.schema.json"
      },
      {
        "fileMatch": [
          "/stryker.conf.json",
          "/stryker-*.conf.json"
        ],
        "uri": "https://raw.githubusercontent.com/stryker-mutator/stryker/master/packages/api/schema/stryker-core.json"
      },
      {
        "fileMatch": [
          "/stylecop.json"
        ],
        "uri": "https://raw.githubusercontent.com/DotNetAnalyzers/StyleCopAnalyzers/master/StyleCop.Analyzers/StyleCop.Analyzers/Settings/stylecop.schema.json"
      },
      {
        "fileMatch": [
          "/.stylelintrc",
          "/.stylelintrc.json"
        ],
        "uri": "https://json.schemastore.org/stylelintrc.json"
      },
      {
        "fileMatch": [
          "/swagger.json"
        ],
        "uri": "https://json.schemastore.org/swagger-2.0.json"
      },
      {
        "fileMatch": [
          "/task.json",
          "/tasks.json"
        ],
        "uri": "https://json.schemastore.org/task.json"
      },
      {
        "fileMatch": [
          "/.talismanrc"
        ],
        "uri": "https://raw.githubusercontent.com/thoughtworks/talisman/main/examples/schema-store-talismanrc.json"
      },
      {
        "fileMatch": [
          "/**/.template.config/template.json"
        ],
        "uri": "https://json.schemastore.org/template.json"
      },
      {
        "fileMatch": [
          "/templatesources.json"
        ],
        "uri": "https://json.schemastore.org/templatesources.json"
      },
      {
        "fileMatch": [
          "/pricing.json"
        ],
        "uri": "https://raw.githubusercontent.com/tierrun/tier/main/pricing/schema.json"
      },
      {
        "fileMatch": [
          "/tikibase.json"
        ],
        "uri": "https://raw.githubusercontent.com/kevgo/tikibase/main/doc/tikibase.schema.json"
      },
      {
        "fileMatch": [
          "/theme.json"
        ],
        "uri": "https://schemas.wp.org/trunk/theme.json"
      },
      {
        "fileMatch": [
          "/.tldr.json"
        ],
        "uri": "https://json.schemastore.org/tldr.json"
      },
      {
        "fileMatch": [
          "/*.tmLanguage.json"
        ],
        "uri": "https://json.schemastore.org/tmlanguage.json"
      },
      {
        "fileMatch": [
          "/testEnvironments.json"
        ],
        "uri": "https://json.schemastore.org/testenvironments.json"
      },
      {
        "fileMatch": [
          "/turbo.json"
        ],
        "uri": "https://turborepo.org/schema.json"
      },
      {
        "uri": "https://json.schemastore.org/traefik-v2-file-provider.json"
      },
      {
        "fileMatch": [
          "/tsconfig*.json"
        ],
        "uri": "https://json.schemastore.org/tsconfig.json"
      },
      {
        "fileMatch": [
          "/tsd.json"
        ],
        "uri": "https://json.schemastore.org/tsd.json"
      },
      {
        "fileMatch": [
          "/.tsdrc"
        ],
        "uri": "https://json.schemastore.org/tsdrc.json"
      },
      {
        "fileMatch": [
          "/ts-force-config.json"
        ],
        "uri": "https://json.schemastore.org/ts-force-config.json"
      },
      {
        "fileMatch": [
          "/tslint.json"
        ],
        "uri": "https://json.schemastore.org/tslint.json"
      },
      {
        "fileMatch": [
          "/*.tson",
          "/*.tson.json"
        ],
        "uri": "https://raw.githubusercontent.com/spectral-discord/TSON/main/schema/tson.json"
      },
      {
        "fileMatch": [
          "/tstyche.config.json"
        ],
        "uri": "https://tstyche.org/schemas/config.json"
      },
      {
        "fileMatch": [
          "/tsup.config.json"
        ],
        "uri": "https://cdn.jsdelivr.net/npm/tsup/schema.json"
      },
      {
        "fileMatch": [
          "/typewiz.json"
        ],
        "uri": "https://json.schemastore.org/typewiz.json"
      },
      {
        "fileMatch": [
          "/typings.json"
        ],
        "uri": "https://json.schemastore.org/typings.json"
      },
      {
        "fileMatch": [
          "/.typingsrc"
        ],
        "uri": "https://json.schemastore.org/typingsrc.json"
      },
      {
        "fileMatch": [
          "/user-data"
        ],
        "uri": "https://json.schemastore.org/ubuntu-server-autoinstall.json"
      },
      {
        "fileMatch": [
          "/up.json"
        ],
        "uri": "https://json.schemastore.org/up.json"
      },
      {
        "fileMatch": [
          "/**/webapp/manifest.json",
          "/**/src/main/webapp/manifest.json",
          "/**/src/manifest.json"
        ],
        "uri": "https://raw.githubusercontent.com/SAP/ui5-manifest/master/schema.json"
      },
      {
        "fileMatch": [
          "/*.utam.json",
          "/.utam.json"
        ],
        "uri": "https://json.schemastore.org/utam-page-object.json"
      },
      {
        "fileMatch": [
          "/*.vg",
          "/*.vg.json"
        ],
        "uri": "https://json.schemastore.org/vega.json"
      },
      {
        "fileMatch": [
          "/*.vl",
          "/*.vl.json"
        ],
        "uri": "https://json.schemastore.org/vega-lite.json"
      },
      {
        "fileMatch": [
          "/venvironment.json",
          "/*.venvironment.json"
        ],
        "uri": "https://json.schemastore.org/venvironment-schema.json"
      },
      {
        "fileMatch": [
          "/venvironment-basic.json",
          "/*.venvironment-basic.json"
        ],
        "uri": "https://json.schemastore.org/venvironment-basic-schema.json"
      },
      {
        "fileMatch": [
          "/version.json"
        ],
        "uri": "https://raw.githubusercontent.com/dotnet/Nerdbank.GitVersioning/master/src/NerdBank.GitVersioning/version.schema.json"
      },
      {
        "fileMatch": [
          "/**/*vim*/addon-info.json"
        ],
        "uri": "https://json.schemastore.org/vim-addon-info.json"
      },
      {
        "fileMatch": [
          "/.vsls.json"
        ],
        "uri": "https://json.schemastore.org/vsls.json"
      },
      {
        "fileMatch": [
          "/vs-2017.3.host.json"
        ],
        "uri": "https://json.schemastore.org/vs-2017.3.host.json"
      },
      {
        "fileMatch": [
          "/*.filenesting.json",
          "/.filenesting.json"
        ],
        "uri": "https://json.schemastore.org/vs-nesting.json"
      },
      {
        "fileMatch": [
          "/*.vsconfig"
        ],
        "uri": "https://json.schemastore.org/vsconfig.json"
      },
      {
        "fileMatch": [
          "/*.vsext"
        ],
        "uri": "https://json.schemastore.org/vsext.json"
      },
      {
        "fileMatch": [
          "/vs-publish.json"
        ],
        "uri": "https://json.schemastore.org/vsix-publish.json"
      },
      {
        "fileMatch": [
          "/vss-extension.json"
        ],
        "uri": "https://json.schemastore.org/vss-extension.json"
      },
      {
        "fileMatch": [
          "/*.vtesttree.json"
        ],
        "uri": "https://json.schemastore.org/vtesttree-schema.json"
      },
      {
        "fileMatch": [
          "/*.vtestunit.json"
        ],
        "uri": "https://json.schemastore.org/vtestunit-schema.json"
      },
      {
        "fileMatch": [
          "/.v8rrc.json"
        ],
        "uri": "https://raw.githubusercontent.com/chris48s/v8r/main/config-schema.json"
      },
      {
        "fileMatch": [
          "/studio.config.json"
        ],
        "uri": "https://webcomponents.dev/assets2/schemas/studio.config.json"
      },
      {
        "fileMatch": [
          "/manifest.json"
        ],
        "uri": "https://json.schemastore.org/webextension.json"
      },
      {
        "fileMatch": [
          "/manifest.json",
          "/*.webmanifest"
        ],
        "uri": "https://json.schemastore.org/web-manifest-combined.json"
      },
      {
        "fileMatch": [
          "/webjobs-list.json"
        ],
        "uri": "https://json.schemastore.org/webjobs-list.json"
      },
      {
        "fileMatch": [
          "/webjobpublishsettings.json"
        ],
        "uri": "https://json.schemastore.org/webjob-publish-settings.json"
      },
      {
        "fileMatch": [
          "/web-types.json",
          "/*.web-types.json"
        ],
        "uri": "https://json.schemastore.org/web-types.json"
      },
      {
        "uri": "https://json-stat.org/format/schema/2.0/"
      },
      {
        "fileMatch": [
          "/*.version"
        ],
        "uri": "https://raw.githubusercontent.com/linuxgurugamer/KSPAddonVersionChecker/master/KSP-AVC.schema.json"
      },
      {
        "fileMatch": [
          "/*.ckan"
        ],
        "uri": "https://raw.githubusercontent.com/KSP-CKAN/CKAN/master/CKAN.schema"
      },
      {
        "uri": "https://json-schema.org/draft-04/schema"
      },
      {
        "fileMatch": [
          "/*.schema.json"
        ],
        "uri": "https://json-schema.org/draft-07/schema"
      },
      {
        "uri": "https://json-schema.org/draft/2019-09/schema"
      },
      {
        "uri": "https://json-schema.org/draft/2020-12/schema"
      },
      {
        "fileMatch": [
          "/xunit.runner.json",
          "/*.xunit.runner.json"
        ],
        "uri": "https://json.schemastore.org/xunit.runner.schema.json"
      },
      {
        "fileMatch": [
          "/*.servicehub.service.json"
        ],
        "uri": "https://json.schemastore.org/servicehub.service.schema.json"
      },
      {
        "fileMatch": [
          "/servicehub.config.json"
        ],
        "uri": "https://json.schemastore.org/servicehub.config.schema.json"
      },
      {
        "fileMatch": [
          "/*.cryproj"
        ],
        "uri": "https://json.schemastore.org/cryproj.52.schema.json"
      },
      {
        "fileMatch": [
          "/*.cryproj"
        ],
        "uri": "https://json.schemastore.org/cryproj.53.schema.json"
      },
      {
        "fileMatch": [
          "/*.cryproj"
        ],
        "uri": "https://json.schemastore.org/cryproj.54.schema.json"
      },
      {
        "fileMatch": [
          "/*.cryproj"
        ],
        "uri": "https://json.schemastore.org/cryproj.55.schema.json"
      },
      {
        "fileMatch": [
          "/*.cryproj"
        ],
        "uri": "https://json.schemastore.org/cryproj.dev.schema.json"
      },
      {
        "fileMatch": [
          "/*.cryproj"
        ],
        "uri": "https://json.schemastore.org/cryproj.json"
      },
      {
        "fileMatch": [
          "/typedoc.json"
        ],
        "uri": "https://typedoc.org/schema.json"
      },
      {
        "fileMatch": [
          "/.huskyrc",
          "/.huskyrc.json"
        ],
        "uri": "https://json.schemastore.org/huskyrc.json"
      },
      {
        "fileMatch": [
          "/.lintstagedrc",
          "/.lintstagedrc.json"
        ],
        "uri": "https://json.schemastore.org/lintstagedrc.schema.json"
      },
      {
        "fileMatch": [
          "/*.mirrord.+(toml|json|y?(a)ml)"
        ],
        "uri": "https://raw.githubusercontent.com/metalbear-co/mirrord/main/mirrord-schema.json"
      },
      {
        "fileMatch": [
          "/motif.json"
        ],
        "uri": "https://motif.land/api/motif.schema.json"
      },
      {
        "fileMatch": [
          "/*.mtaext"
        ],
        "uri": "https://json.schemastore.org/mtaext.json"
      },
      {
        "fileMatch": [
          "/xs-app.json"
        ],
        "uri": "https://json.schemastore.org/xs-app.json"
      },
      {
        "fileMatch": [
          "/hemtt.json"
        ],
        "uri": "https://json.schemastore.org/hemtt-0.6.2.json"
      },
      {
        "fileMatch": [
          "/now.json"
        ],
        "uri": "https://json.schemastore.org/now.json"
      },
      {
        "fileMatch": [
          "/BizTalkServerInventory.json"
        ],
        "uri": "https://json.schemastore.org/BizTalkServerApplicationSchema.json"
      },
      {
        "fileMatch": [
          "/.httpmockrc",
          "/.httpmock.json"
        ],
        "uri": "https://json.schemastore.org/httpmockrc.json"
      },
      {
        "fileMatch": [
          "/.nl.json",
          "/.neoload.json"
        ],
        "uri": "https://raw.githubusercontent.com/Neotys-Labs/neoload-cli/master/resources/as-code.latest.schema.json"
      },
      {
        "fileMatch": [
          "/*.har"
        ],
        "uri": "https://raw.githubusercontent.com/ahmadnassri/har-schema/master/lib/har.json"
      },
      {
        "fileMatch": [
          "/conf.js*",
          "/jsdoc.js*"
        ],
        "uri": "https://json.schemastore.org/jsdoc-1.0.0.json"
      },
      {
        "fileMatch": [
          "/.commitlintrc",
          "/.commitlintrc.json"
        ],
        "uri": "https://json.schemastore.org/commitlintrc.json"
      },
      {
        "fileMatch": [
          "/*.tokenlist.json"
        ],
        "uri": "https://uniswap.org/tokenlist.schema.json"
      },
      {
        "fileMatch": [
          "/**/.yamllint"
        ],
        "uri": "https://json.schemastore.org/yamllint.json"
      },
      {
        "fileMatch": [
          "/devinit.json",
          "/.devinit.json"
        ],
        "uri": "https://json.schemastore.org/devinit.schema-6.0.json"
      },
      {
        "fileMatch": [
          "/.djlintrc"
        ],
        "uri": "https://json.schemastore.org/djlint.json"
      },
      {
        "fileMatch": [
          "/**/tsoa.json"
        ],
        "uri": "https://json.schemastore.org/tsoa.json"
      },
      {
        "fileMatch": [
          "/**/api.json"
        ],
        "uri": "https://json.schemastore.org/apibuilder.json"
      },
      {
        "fileMatch": [
          "/.swcrc"
        ],
        "uri": "https://json.schemastore.org/swcrc.json"
      },
      {
        "uri": "https://json.schemastore.org/openweather.roadrisk.json"
      },
      {
        "uri": "https://json.schemastore.org/openweather.current.json"
      },
      {
        "uri": "https://json.schemastore.org/jsone.json"
      },
      {
        "fileMatch": [
          "/cluster.json"
        ],
        "uri": "https://raw.githubusercontent.com/dcermak/vscode-rke-cluster-config/main/schemas/cluster.json"
      },
      {
        "fileMatch": [
          "/**/db/changelog/**/*.json"
        ],
        "uri": "https://json.schemastore.org/liquibase-3.2.json"
      },
      {
        "fileMatch": [
          "/kfp_component.json"
        ],
        "uri": "https://raw.githubusercontent.com/Cloud-Pipelines/component_spec_schema/stable/component_spec.json_schema.json"
      },
      {
        "fileMatch": [
          "/.markdownlintrc",
          "/.markdownlint.json",
          "/.markdownlint.jsonc"
        ],
        "uri": "https://raw.githubusercontent.com/DavidAnson/markdownlint/main/schema/markdownlint-config-schema.json"
      },
      {
        "fileMatch": [
          "/.markdown-lint-check.json"
        ],
        "uri": "https://json.schemastore.org/markdown-lint-check.json"
      },
      {
        "fileMatch": [
          "/*.ndst.json"
        ],
        "uri": "https://s3.eu-central-1.amazonaws.com/files.netin.io/spider-schemas/template.schema.json"
      },
      {
        "uri": "https://json.schemastore.org/scikit-build.json"
      },
      {
        "fileMatch": [
          "/*.sw.json"
        ],
        "uri": "https://raw.githubusercontent.com/serverlessworkflow/specification/main/schema/workflow.json"
      },
      {
        "uri": "https://json.schemastore.org/unist.json"
      },
      {
        "fileMatch": [
          "/hugo.json"
        ],
        "uri": "https://json.schemastore.org/hugo.json"
      },
      {
        "fileMatch": [
          "/.deployedrc",
          "/.deployed.json"
        ],
        "uri": "https://json.schemastore.org/deployed.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/statelyai/xstate/main/packages/core/src/machine.schema.json"
      },
      {
        "fileMatch": [
          "/*.bu"
        ],
        "uri": "https://raw.githubusercontent.com/Relativ-IT/Butane-Schemas/Release/Butane-Schema.json"
      },
      {
        "fileMatch": [
          "/**/updatecli.d/**/*.json"
        ],
        "uri": "https://www.updatecli.io/schema/latest/policy/manifest/config.json"
      },
      {
        "uri": "https://geojson.org/schema/GeoJSON.json"
      },
      {
        "fileMatch": [
          "/.clang-format"
        ],
        "uri": "https://json.schemastore.org/clang-format.json"
      },
      {
        "fileMatch": [
          "/flow.json",
          "/*.flow.json"
        ],
        "uri": "https://raw.githubusercontent.com/estuary/flow/master/flow.schema.json"
      },
      {
        "fileMatch": [
          "/**/v2ray/*.json"
        ],
        "uri": "https://raw.githubusercontent.com/EHfive/v2ray-jsonschema/main/v4-config.schema.json"
      },
      {
        "fileMatch": [
          "/.gherking.json",
          "/.gherkingrc",
          "/.gherking.js",
          "/gherking.json",
          "/gherking.js"
        ],
        "uri": "https://raw.githubusercontent.com/gherking/gherking/master/schema/gherking.schema.json"
      },
      {
        "fileMatch": [
          "/.hintrc"
        ],
        "uri": "https://raw.githubusercontent.com/webhintio/hint/main/packages/hint/src/lib/config/config-schema.json"
      },
      {
        "fileMatch": [
          "/ava.config.json"
        ],
        "uri": "https://json.schemastore.org/ava.json"
      },
      {
        "fileMatch": [
          "/*.dhub.json"
        ],
        "uri": "https://datahubproject.io/schemas/datahub_ingestion_schema.json"
      },
      {
        "fileMatch": [
          "/.jscpd.json"
        ],
        "uri": "https://json.schemastore.org/jscpd.json"
      },
      {
        "fileMatch": [
          "/egg-*.json"
        ],
        "uri": "https://json.schemastore.org/pterodactyl.json"
      },
      {
        "fileMatch": [
          "/monika.json"
        ],
        "uri": "https://json.schemastore.org/monika-config-schema.json"
      },
      {
        "fileMatch": [
          "/.nycrc",
          "/.nycrc.json"
        ],
        "uri": "https://json.schemastore.org/nycrc.json"
      },
      {
        "fileMatch": [
          "/*-index.json"
        ],
        "uri": "https://json.schemastore.org/mongodb-atlas-search-index-definition.json"
      },
      {
        "fileMatch": [
          "/embrace-config.json"
        ],
        "uri": "https://json.schemastore.org/embrace-config-schema-1.0.0.json"
      },
      {
        "fileMatch": [
          "/petstore-v1.0.json"
        ],
        "uri": "https://json.schemastore.org/petstore-v1.0.json"
      },
      {
        "fileMatch": [
          "/*batch-job-config*.json"
        ],
        "uri": "https://json-schemaMir.api.strmprivacy.io/latest/strmprivacyMir.api.entities.v1.BatchJob.json"
      },
      {
        "fileMatch": [
          "/*simple-schema*.json"
        ],
        "uri": "https://json-schemaMir.api.strmprivacy.io/latest/strmprivacyMir.api.entities.v1.Schema.SimpleSchemaDefinition.json"
      },
      {
        "fileMatch": [
          "/*stream.json"
        ],
        "uri": "https://json-schemaMir.api.strmprivacy.io/latest/strmprivacyMir.api.entities.v1.Stream.json"
      },
      {
        "fileMatch": [
          "/*data-connector.json"
        ],
        "uri": "https://json-schemaMir.api.strmprivacy.io/latest/strmprivacyMir.api.entities.v1.DataConnector.json"
      },
      {
        "fileMatch": [
          "/*contract.json"
        ],
        "uri": "https://json-schemaMir.api.strmprivacy.io/latest/strmprivacyMir.api.entities.v1.DataContract.json"
      },
      {
        "fileMatch": [
          "/*.sublime-syntax"
        ],
        "uri": "https://json.schemastore.org/sublime-syntax.json"
      },
      {
        "uri": "https://raw.githubusercontent.com/dahag-ag/keycloak-openapi/main/OpenApiDefinitions/keycloak-19.0.0.json"
      },
      {
        "fileMatch": [
          "/qfconfig.json"
        ],
        "uri": "https://json.schemastore.org/qfconfig.json"
      },
      {
        "fileMatch": [
          "/BuildConfig.json"
        ],
        "uri": "https://raw.githubusercontent.com/RedpointGames/uet-schema/main/root.json"
      },
      {
        "fileMatch": [
          "/.uplugin"
        ],
        "uri": "https://json.schemastore.org/uplugin.json"
      },
      {
        "fileMatch": [
          "/.uproject"
        ],
        "uri": "https://json.schemastore.org/uproject.json"
      },
      {
        "fileMatch": [
          "/.all-contributorsrc"
        ],
        "uri": "https://json.schemastore.org/all-contributors.json"
      },
      {
        "fileMatch": [
          "/.es6importsorterrc.json"
        ],
        "uri": "https://json.schemastore.org/es6importsorterrc.json"
      },
      {
        "fileMatch": [
          "/bpkg.json"
        ],
        "uri": "https://json.schemastore.org/bpkg.json"
      },
      {
        "fileMatch": [
          "/**/.config/micro/settings.json"
        ],
        "uri": "https://raw.githubusercontent.com/zyedidia/micro/master/data/micro.json"
      },
      {
        "fileMatch": [
          "/quilt.mod.json"
        ],
        "uri": "https://raw.githubusercontent.com/QuiltMC/quilt-json-schemas/main/quilt.mod.json/schemas/main.json"
      },
      {
        "fileMatch": [
          "/.aliases"
        ],
        "uri": "https://json.schemastore.org/aliases.json"
      },
      {
        "fileMatch": [
          "/custom-elements.json"
        ],
        "uri": "https://raw.githubusercontent.com/webcomponents/custom-elements-manifest/main/schema.json"
      },
      {
        "fileMatch": [
          "/**/.goblet/config.json"
        ],
        "uri": "https://raw.githubusercontent.com/goblet/goblet/main/goblet.schema.json"
      },
      {
        "uri": "https://json.schemastore.org/metaschema-draft-07-unofficial-strict.json"
      },
      {
        "fileMatch": [
          "/*.ki.json"
        ],
        "uri": "https://enduricastorage.blob.core.windows.net/public/endurica-cl-schema.json"
      }
    ]
