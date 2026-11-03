# Minimal AWS provider. Credentials and endpoints come from the standard AWS
# configuration. Do NOT hardcode endpoints here; `tflocal` injects them when
# targeting a local emulator.
#
# s3_use_path_style keeps emulator URLs resolvable (http://host:4566/videos);
# real S3 accepts path-style requests too.
provider "aws" {
  region            = var.aws_region
  s3_use_path_style = true
}
