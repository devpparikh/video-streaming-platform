variable "aws_region" {
  description = "AWS region to create the resources in."
  type        = string
  default     = "us-east-1"
}

variable "videos_bucket_name" {
  description = "Name of the S3 bucket that stores raw uploads and DASH output."
  type        = string
  default     = "videos"
}

variable "cors_allowed_origins" {
  description = "Origins allowed to read objects directly from the bucket (e.g. the frontend dev server)."
  type        = list(string)
  default     = ["http://localhost:5173"]
}

variable "videos_table_name" {
  description = "DynamoDB table holding the source-of-truth metadata per video."
  type        = string
  default     = "video-metadata"
}
