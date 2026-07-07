import re
import textwrap

def extract_variable(content, var_name, is_array=False):
    if is_array:
        pattern = re.compile(rf'^{var_name}=\(([^)]+)\)', re.MULTILINE)
        match = pattern.search(content)
        if match:
            cleaned = re.sub(r'["\']', '', match.group(1))
            return [item.strip() for item in cleaned.split()]
        return []
    else:
        pattern = re.compile(rf'^{var_name}=([^\n]+)', re.MULTILINE)
        match = pattern.search(content)
        if match:
            return match.group(1).strip('"\' ')
        return ""

def extract_function(content, func_name):
    if func_name == 'package':
        pattern = re.compile(r'^package(?:_[a-zA-Z0-9_-]+)?\s*\(\)\s*\{', re.MULTILINE)
    else:
        pattern = re.compile(rf'^{func_name}\s*\(\)\s*\{{', re.MULTILINE)
        
    match = pattern.search(content)
    if not match:
        return ""
    
    start = match.end()
    brace_count = 1
    i = start
    
    while i < len(content) and brace_count > 0:
        if content[i] == '{':
            brace_count += 1
        elif content[i] == '}':
            brace_count -= 1
        i += 1
        
    body = textwrap.dedent(content[start:i-1]).strip()
    
    body = (body
            .replace('"${pkgdir}"', '"$DESTDIR"')
            .replace('"$pkgdir"', '"$DESTDIR"')
            .replace('${pkgdir}', '$DESTDIR')
            .replace('$pkgdir', '$DESTDIR')
            .replace('"${pkgname}"', '"$PKG_NAME"')
            .replace('"$pkgname"', '"$PKG_NAME"')
            .replace('${pkgname}', '$PKG_NAME')
            .replace('$pkgname', '$PKG_NAME')
            .replace('"${pkgbase}"', '"$PKG_NAME"')
            .replace('"$pkgbase"', '"$PKG_NAME"')
            .replace('${pkgbase}', '$PKG_NAME')
            .replace('$pkgbase', '$PKG_NAME')
            .replace('"${pkgver}"', '"$PKG_VERSION"')
            .replace('"$pkgver"', '"$PKG_VERSION"')
            .replace('${pkgver}', '$PKG_VERSION')
            .replace('$pkgver', '$PKG_VERSION'))
    
    body = re.sub(r'^.*cd\s+.*\$srcdir.*$', '', body, flags=re.MULTILINE).strip()
    return body

def generate_freeside_package(pkgbuild_content):
    pkgname = extract_variable(pkgbuild_content, 'pkgname')
    if not pkgname:
        pkgbase = extract_variable(pkgbuild_content, 'pkgbase')
        if pkgbase:
            pkgname = pkgbase
        else:
            pkgname_array = extract_variable(pkgbuild_content, 'pkgname', is_array=True)
            pkgname = pkgname_array[0] if pkgname_array else ""

    pkgver = extract_variable(pkgbuild_content, 'pkgver')
    pkgdesc = extract_variable(pkgbuild_content, 'pkgdesc')
    depends = extract_variable(pkgbuild_content, 'depends', is_array=True)
    sources = extract_variable(pkgbuild_content, 'source', is_array=True)
    sha256sums = extract_variable(pkgbuild_content, 'sha256sums', is_array=True)
    
    primary_source = sources[0] if sources else ""
    primary_source = (primary_source
                      .replace('"${pkgname}"', pkgname)
                      .replace('"$pkgname"', pkgname)
                      .replace('${pkgname}', pkgname)
                      .replace('$pkgname', pkgname)
                      .replace('"${pkgbase}"', pkgname)
                      .replace('"$pkgbase"', pkgname)
                      .replace('${pkgbase}', pkgname)
                      .replace('$pkgbase', pkgname)
                      .replace('"${pkgver}"', pkgver)
                      .replace('"$pkgver"', pkgver)
                      .replace('${pkgver}', pkgver)
                      .replace('$pkgver', pkgver))
    primary_sha = sha256sums[0] if sha256sums and sha256sums[0] != 'SKIP' else ""
    
    depends_str = '[]' if not depends else '["' + '", "'.join(depends) + '"]'
    
    manifest_toml = textwrap.dedent(f"""\
        [package]
        name = "{pkgname}"
        version = "{pkgver}"
        description = "{pkgdesc}"
        dependencies = {depends_str}

        [[sources]]
        url = "{primary_source}"
        checksum = {{ algorithm = "sha256", value = "{primary_sha}" }}
    """)
    
    build_body = extract_function(pkgbuild_content, 'build')
    package_body = extract_function(pkgbuild_content, 'package')
    
    justfile = ""
    if build_body:
        justfile += "build:\n"
        justfile += textwrap.indent(build_body, "    ") + "\n\n"
        
    if package_body:
        justfile += "package:\n"
        justfile += textwrap.indent(package_body, "    ") + "\n"
    
    return manifest_toml, justfile.strip(), pkgver, primary_source, primary_sha
