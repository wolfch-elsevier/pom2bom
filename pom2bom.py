#!/usr/bin/env python
"""
Scans all the child POMs and extracts dependency versions, removing the "<version/>" from
the child POMs and putting into the parent "<dependencyManagement/>", i.e. the B.O.M.

If the same dependency has different versions, then "packaging.version.parse_version" is used
to compare and select highest version.

The new parent and child POMs are written as "pom_new.xml".
"""
import logging
import os
import re
import xml.etree.cElementTree as ET

# import lxml.etree as ET
from collections import OrderedDict
from xml.dom import Node, minidom

# from xml.etree.ElementTree import ParseError

from packaging.version import parse as parse_version
from packaging.version import InvalidVersion

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-6s %(message)s",
    datefmt="%m-%d %H:%M",
)
log = logging.getLogger("pom2bom")

MVN_NS = "http://maven.apache.org/POM/4.0.0"
# XML namespace map for ElementTree
NS_MAP = {"mvn": MVN_NS}
SAFE_TEXT = lambda elem: elem.text.strip() if elem.text else ""
INTERPOLATE_PAT = re.compile(r"\$\{([\w.-]+)\}")


class POMScanner:
    def __init__(self, pom_path) -> None:
        self.tree = ET.parse(pom_path)
        self.project = self.tree.getroot()
        # self.versionprop = {}
        self.dependency_groups = {}
        self.non_version_props = {}
        self.version_props = {}
        # self.scan_for_dependencies()

    def scan_for_version_properties(self):
        properties = self.project.find("mvn:properties", NS_MAP)
        if not properties:
            return
        for property in properties:
            local_tag = localname(property.tag)
            if local_tag.lower().find("version") > -1:
                if property.text:
                    self.version_props[local_tag] = SAFE_TEXT(property)
            else:
                if property.text:
                    self.non_version_props[local_tag] = SAFE_TEXT(property)

    def scan_for_dependencies(self, project_properties: dict):
        """This routine can be called separately just for getting a dependency list from a POM"""
        self.scan_for_version_properties()
        for dependency in self.project.iter("{" + MVN_NS + "}dependency"):
            record = {}
            for elem in dependency:
                field_name = localname(elem.tag)
                if elem.text.strip().startswith("${"):
                    field_value = interpolate_value(
                        self.version_props, elem.text.strip()
                    )
                    field_value = interpolate_value(project_properties, field_value)
                else:
                    field_value = elem.text.strip()
                record[field_name] = field_value
            if record["groupId"] not in self.dependency_groups:
                self.dependency_groups[record["groupId"]] = {}

            self.dependency_groups[record["groupId"]][record["artifactId"]] = (
                record["version"] if "version" in record else None
            )

    """
    def render_version(self, version_str):
        try:
            version_mo = self.INTERPOLATE_PAT.match(version_str)
            version_str_out = (
                self.version_props[version_mo[1]] if version_mo else version_str
            )
            return version_str_out
        except KeyError:
            return version_str
    """


def interpolate_value(values_map: dict, value_str: str) -> str:
    """If value_str is '${something}' use 'something' to lookup in value_map"""
    try:
        value_mo = INTERPOLATE_PAT.match(value_str)
        value_str_out = values_map[value_mo[1]] if value_mo else value_str
        return value_str_out
    except KeyError:
        return value_str


def localname(tag_name):
    """chops off the bracketed prefix namespace URI"""
    if tag_name.find("}") == -1:  # no namespace
        return tag_name
    return tag_name[tag_name.find("}") + 1 :]


def strip_pom_file(input_pom_path, output_pom_path, properties_to_strip):
    """Will remove "<version/>" and "<dependencyManagement/>"
    from child POMs as well as "<property/>" that should only be
    defined in the parent, e.g. "<project.build.sourceEncoding/>"
    """
    tree = minidom.parse(input_pom_path)
    elem = tree.documentElement

    def walk(node):
        if (
            node.nodeType == Node.ELEMENT_NODE
            and node.nodeName == "version"
            and node.parentNode.nodeName == "dependency"
        ):
            node.parentNode.removeChild(node)
            node.unlink()
            return

        if (
            node.nodeType == Node.ELEMENT_NODE
            and node.nodeName == "dependencyManagement"
            and node.parentNode.nodeName == "project"
        ):
            node.parentNode.removeChild(node)
            node.unlink()
            return

        for pn, pv in properties_to_strip.items():
            if (
                node.nodeType == Node.ELEMENT_NODE
                and node.nodeName == pn
                and node.parentNode.nodeName == "properties"
            ):
                node.parentNode.removeChild(node)
                node.unlink()

        for cn in node.childNodes:
            walk(cn)

    walk(elem)

    with open(output_pom_path, "w", encoding="utf-8") as fh:
        elem.writexml(fh)


def update_dependencies(
    project_properties, current_dependencies, new_dependencies, module_name
):
    for group_id in new_dependencies.keys():
        group_id = interpolate_value(project_properties, group_id)
        if group_id not in current_dependencies:
            current_dependencies[group_id] = new_dependencies[group_id]
            log.info("%s: Found new dependency group %s", module_name, group_id)
            continue
        for artifact, artifact_version in new_dependencies[group_id].items():
            if not artifact_version:
                continue
            if artifact not in current_dependencies[group_id]:
                log.info(
                    "%s: Found new dependency %s:%s:%s",
                    module_name,
                    group_id,
                    artifact,
                    artifact_version,
                )
                current_dependencies[group_id][artifact] = artifact_version
                continue
            if current_dependencies[group_id][artifact] == None:
                log.warning(
                    "%s: %s:%s BOM version overriden with %s",
                    module_name,
                    group_id,
                    artifact,
                    artifact_version,
                )
                current_dependencies[group_id][artifact] = artifact_version
                continue
            try:
                # replace if higher version of same dependency found...
                if parse_version(artifact_version) > parse_version(
                    current_dependencies[group_id][artifact]
                ):
                    log.info(
                        "%s: %s:%s - replaced version %s with %s",
                        module_name,
                        group_id,
                        artifact,
                        current_dependencies[group_id][artifact],
                        artifact_version,
                    )
                    current_dependencies[group_id][artifact] = artifact_version
            except InvalidVersion as e:
                log.warning(
                    "Not replacing incomparable version, %s for %s:%s",
                    artifact_version,
                    group_id,
                    artifact,
                )


def insert_bom_into_parent_pom(base_dir, parent_pom_doc, properties, dependencies):
    """Adds "<dependencyManagement/>" section to parent POM"""
    root = parent_pom_doc.getroot()
    properties_element = root.find(".//mvn:properties", NS_MAP)
    for key in sorted(list(properties)):
        ET.SubElement(properties_element, key).text = properties[key]

    artifacts = OrderedDict()
    for group_id in dependencies:
        for artifact in dependencies[group_id]:
            artifacts[artifact] = ""

    child_count = len(list(root))
    dm_element = ET.Element("dependencyManagement")
    dep_element = ET.SubElement(dm_element, "dependencies")
    for group_id in sorted(list(dependencies)):
        for artifact in sorted(list(dependencies[group_id])):
            dependency = ET.SubElement(dep_element, "dependency")
            ET.SubElement(dependency, "groupId").text = group_id
            ET.SubElement(dependency, "artifactId").text = artifact
            ET.SubElement(dependency, "version").text = "${" + artifact + ".version}"
            artifacts[artifact] = dependencies[group_id][artifact]

    for artifact in artifacts:
        ET.SubElement(properties_element, artifact + ".version").text = artifacts[
            artifact
        ]

    root.insert(child_count, dm_element)
    ET.indent(parent_pom_doc, "  ")
    parent_pom_doc.write(
        os.path.join(base_dir, "pom_new.xml"), encoding="UTF-8", xml_declaration=False
    )


def scan_and_create_bom(base_dir):
    """main entry point"""
    properties = {}
    dependencies = {}

    ET.register_namespace("", MVN_NS)  # set default namespace

    pomscanner = POMScanner(os.path.join(base_dir, "pom.xml"))
    parent_pom_doc = pomscanner.tree
    project = pomscanner.project

    project_groupid = project.find("mvn:groupId", NS_MAP)
    if project_groupid is not None:
        properties["project.groupId"] = SAFE_TEXT(project_groupid)

    project_version = project.find("mvn:version", NS_MAP)
    if project_version is not None:
        properties["project.version"] = SAFE_TEXT(project_version)

    pomscanner.scan_for_version_properties()
    properties.update(pomscanner.version_props)

    mvn_modules = project.findall(".//mvn:module", NS_MAP)
    for mvn_module in mvn_modules:
        mvn_module_pom = os.path.join(base_dir, mvn_module.text, "pom.xml")
        if os.path.exists(mvn_module_pom):
            new_mvn_module_pom = os.path.join(base_dir, mvn_module.text, "pom_new.xml")
            pomscanner = POMScanner(mvn_module_pom)
            pomscanner.scan_for_dependencies(properties)
            update_dependencies(
                properties, dependencies, pomscanner.dependency_groups, mvn_module.text
            )
            properties.update(pomscanner.version_props)
            properties_to_strip = {}
            properties_to_strip.update(pomscanner.non_version_props)
            properties_to_strip.update(pomscanner.version_props)
            strip_pom_file(mvn_module_pom, new_mvn_module_pom, properties_to_strip)

    insert_bom_into_parent_pom(base_dir, parent_pom_doc, properties, dependencies)


if __name__ == "__main__":
    BASEDIR = "/Users/wolf2/src6/Search"

    scan_and_create_bom(BASEDIR)

    """
    # Example read-only way to scan individual pom.xml for dependency list...
    pomscanner = POMScanner("pom.xml")
    for groupId, artifacts in pomscanner.dependencies.items():
        print(f"{groupId}: {str(artifacts)}")
    """
