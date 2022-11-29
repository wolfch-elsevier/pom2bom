#!/usr/bin/env python
import logging
import os
import re
import sys
from collections import OrderedDict
from packaging.version import parse as parse_version
from xml.dom import minidom
from xml.dom import Node
import xml.etree.cElementTree as ET
from xml.etree.ElementTree import ParseError

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-6s %(message)s",
    datefmt="%m-%d %H:%M",
)
log = logging.getLogger("pom2bom")

MVN_NS = "http://maven.apache.org/POM/4.0.0"
# XML namespace map for ElementTree
ns = {"mvn": MVN_NS}


class POMScanner:
    INTERPOLATE_PAT = re.compile(r"\$\{([\w.-]+)\}")

    def __init__(self, pom_path) -> None:
        tree = ET.parse(pom_path)
        self.project = tree.getroot()
        #self.versionprop = {}
        self.dependency_groups = {}
        self.non_version_props = {}
        self.version_props = {}
        self.scan_for_dependencies()
    
    """
    @property
    def dependencies(self):
        return self.dependency_groups

    @property
    def non_version_props(self):
        return self.non_version_props

    @property
    def version_props(self):
        return self.version_props
    """
    
    def scan_for_version_properties(self):
        properties = self.project.find("mvn:properties", ns)
        if not properties:
            return
        for property in properties:
            local_tag = localname(property.tag)
            if local_tag.lower().find("version") > -1:
                self.version_props[local_tag] = property.text.strip()
            else:
                self.non_version_props[local_tag] = property.text.strip()

    def scan_for_dependencies(self):
        self.scan_for_version_properties()
        for dependency in self.project.iter(
            "{" + MVN_NS + "}dependency"
        ):
            record = {}
            for elem in dependency:
                field_name = localname(elem.tag)
                record[field_name] = (
                    self.render_version(elem.text.strip())
                    if field_name == "version"
                    else elem.text.strip()
                )
            if record["groupId"] not in self.dependency_groups:
                self.dependency_groups[record["groupId"]] = {}

            self.dependency_groups[record["groupId"]][record["artifactId"]] = (
                record["version"] if "version" in record else None
            )

    def render_version(self, version_str):
        version_mo = self.INTERPOLATE_PAT.match(version_str)
        version_str_out = self.version_props[version_mo[1]] if version_mo else version_str
        return version_str_out


def localname(tag_name):
    if tag_name.find("}") == -1:  # no namespace
        return tag_name
    return tag_name[tag_name.find("}") + 1 :]


def strip_pom_file(input_pom_path, output_pom_path, properties_to_strip):
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


def update_dependencies(current_dependencies, new_dependencies, module_name):
    for group_id in new_dependencies.keys():
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

def insert_bom_into_parent_pom(base_dir, parent_pom_doc, properties, dependencies):
    root = parent_pom_doc.getroot()
    properties_element = root.find(".//mvn:properties", ns)
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
        ET.SubElement(properties_element, artifact + ".version").text = artifacts[artifact]
                  
    root.insert(child_count, dm_element)
    parent_pom_doc.write(os.path.join(base_dir, "pom_new.xml"), encoding="UTF-8", xml_declaration=False)


def scan_and_create_bom(base_dir):
    properties = {}
    dependencies = {}

    ET.register_namespace("", MVN_NS) # set default namespace
    parent_pom_doc = ET.parse(os.path.join(base_dir, "pom.xml"))
    root = parent_pom_doc.getroot()
   
    mvn_modules = root.findall(".//mvn:module", ns)
    for mvn_module in mvn_modules:
        mvn_module_pom = os.path.join(base_dir, mvn_module.text, "pom.xml")
        if os.path.exists(mvn_module_pom):
            new_mvn_module_pom = os.path.join(base_dir, mvn_module.text, "pom_new.xml")
            pomscanner = POMScanner(mvn_module_pom)
            update_dependencies(dependencies, pomscanner.dependency_groups, mvn_module.text)
            properties.update(pomscanner.version_props)
            properties_to_strip = {}
            properties_to_strip.update(pomscanner.non_version_props)
            properties_to_strip.update(pomscanner.version_props)
            strip_pom_file(mvn_module_pom, new_mvn_module_pom, properties_to_strip)
       
    insert_bom_into_parent_pom(base_dir, parent_pom_doc, properties, dependencies)


if __name__ == "__main__":
    BASEDIR = "/Users/ma-wolf2/src3/search-sparks-integration"

    scan_and_create_bom(BASEDIR)
    """
    pomscanner = POMScanner("pylib/emclib/src/emclib/pom.xml")
    for groupId, artifacts in pomscanner.dependencies.items():
        print(f"{groupId}: {str(artifacts)}")
    """
    z = 0
